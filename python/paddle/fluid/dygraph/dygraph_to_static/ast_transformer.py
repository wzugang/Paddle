#   Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import print_function

import copy
import inspect
import textwrap

import astor
# gast is a generic AST to represent Python2 and Python3's Abstract Syntax Tree(AST).
# It provides a compatibility layer between the AST of various Python versions,
# as produced by ast.parse from the standard ast module.
# See details in https://github.com/serge-sans-paille/gast/
import gast

from paddle.fluid import unique_name
from paddle.fluid.dygraph.dygraph_to_static.loop_transformer import LoopTransformer
from .ast_utils import is_control_flow_if, create_cond_node, transform_if_else, ast_to_func
from .static_analysis import AstNodeWrapper, NodeVarType, StaticAnalysisVisitor
from .utils import *

__all__ = ['DygraphToStaticAst', 'convert_to_static']

DECORATOR_NAMES = ['dygraph_to_static_output', 'dygraph_to_static_graph']


class IfElseTransformer(gast.NodeTransformer):
    """
    Transform if/else statement of Dygraph into Static Graph.
    """

    def __init__(self, wrapper_root):
        assert isinstance(
            wrapper_root, AstNodeWrapper
        ), "Type of input node should be AstNodeWrapper, but received %s ." % type(
            wrapper_root)
        self.root = wrapper_root.node
        self.static_analysis_visitor = StaticAnalysisVisitor(self.root)
        self.new_func_nodes = {}

    def transform(self):
        """
        Main function to transform AST.
        """
        self.visit(self.root)
        self.after_visit(self.root)

    def visit_If(self, node):
        assert isinstance(node, gast.If)
        need_transform = is_control_flow_if(node.test,
                                            self.static_analysis_visitor)
        self.generic_visit(node)
        if need_transform:
            pred_node = node.test
            true_func_node, false_func_node, return_name_ids = transform_if_else(
                node, self.root)
            # create layers.cond
            new_node = create_cond_node(return_name_ids, pred_node,
                                        true_func_node, false_func_node)
            self.new_func_nodes[new_node] = [true_func_node, false_func_node]
            return new_node
        else:
            return node

    def visit_Call(self, node):
        # Remove `numpy()` statement, like `Tensor.numpy()[i]` -> `Tensor[i]`
        # TODO: should be removed. it may be considered as basic api transformation.
        if isinstance(node.func, gast.Attribute):
            attribute = node.func
            if attribute.attr == 'numpy':
                node = attribute.value
        return node

    def after_visit(self, node):
        """
        This function will add some postprocessing operations with node.
        It can be used to add the created `true_fn/false_fn` in front of
        the node.body before they are called in cond layer.
        """
        self._insert_func_nodes(node)

    def _insert_func_nodes(self, parent_node):
        """
        Defined `true_func` and `false_func` will be inserted in front of corresponding
        `layers.cond` statement instead of inserting them all into body of parent node.
        Because private variables of class or other external scope will be modified.
        For example, `self.var_dict["key"]`. In this case, nested structure of newly
        defined functions is easier to understand.
        """
        if not (self.new_func_nodes and hasattr(parent_node, 'body')):
            return
        idx = len(parent_node.body) - 1
        while idx >= 0:
            child_node = parent_node.body[idx]
            if child_node in self.new_func_nodes:
                parent_node.body[idx:idx] = self.new_func_nodes[child_node]
                idx = idx + len(self.new_func_nodes[child_node]) - 1
                del self.new_func_nodes[child_node]
            else:
                self._insert_func_nodes(child_node)
                idx = idx - 1

    def get_new_func_nodes(self):
        return self.new_func_nodes


class DygraphToStaticAst(gast.NodeTransformer):
    """
    Main class to transform Dygraph to Static Graph
    """

    def get_static_ast(self, root):
        # save root for some analysis may need global AST
        self.root = root
        self.static_analysis_visitor = StaticAnalysisVisitor(root)
        self.static_analysis_root = self.static_analysis_visitor.get_node_wrapper_root(
        )

        self.decorate_func_name = None
        self.arg_name_to_idx = {}
        self.transfer_from_node_type(self.static_analysis_root)
        return self.static_analysis_root

    def transfer_from_node_type(self, node_wrapper):
        # Generic transformation
        self.visit(node_wrapper.node)

        # Transform basic api of dygraph to static graph
        basic_api_trans = BasicApiTransformer(node_wrapper,
                                              self.static_analysis_visitor)
        basic_api_trans.ast_visit()
        self.feed_name_to_arg_name = basic_api_trans.get_feed_name_to_arg_id()

        # Transform all if/else statement of Dygraph into Static Graph.
        IfElseTransformer(node_wrapper).transform()

        LoopTransformer(node_wrapper).transform()

    def visit_FunctionDef(self, node):
        if self.decorate_func_name is None:
            self.decorate_func_name = node.name
            for idx, arg in enumerate(node.args.args):
                self.arg_name_to_idx[arg.id] = idx

        self.generic_visit(node)
        # Remove the decorated name of dygraph_to_static
        if hasattr(node, 'decorator_list'):
            decorator_list = [
                d for d in node.decorator_list if d.id not in DECORATOR_NAMES
            ]
            node.decorator_list = decorator_list
        return node

    def get_module_name(self):
        """
        Return the main function name which will be used as module name
        in ast_to_func.
        """
        # Should consider BaseAPITransformer which add new module name in Yamei's PR.
        assert self.decorate_func_name, "decorate_func_name shall not be None."
        return self.decorate_func_name

    def get_feed_name_to_idx(self):
        feed_name_to_idx = {}
        for feed_name, arg_name in self.feed_name_to_arg_name.items():
            feed_name_to_idx[feed_name] = self.arg_name_to_idx.get(arg_name)
        return feed_name_to_idx


class BasicApiTransformer(gast.NodeTransformer):
    """
    Class to transform basic API from dygraph to static graph.
    """

    def __init__(self, wrapper_root, static_analysis_visitor):
        assert isinstance(
            wrapper_root, AstNodeWrapper
        ), "Input non-AstNodeWrapper node for the initialization of BasicApiTransformer."

        self.wrapper_root = wrapper_root
        self.root = wrapper_root.node
        self.class_node_dict = {}

        # Used for transformation of data feed
        self.feed_name_to_arg_id = {}
        self.name_to_tensor_shape = {}

        # Used for transformation of Tensor.shape
        self.static_analysis_visitor = static_analysis_visitor
        self.node_to_wrapper_map = self.static_analysis_visitor.get_node_to_wrapper_map(
        )
        self.scope_var_type_dict = {}
        self._run_static_visitor()

    def _run_static_visitor(self):
        var_env = copy.deepcopy(self.static_analysis_visitor.get_var_env())
        # TODO: Consider that Tensor.shape is used in sub function and sub_scopes is empty
        var_env.cur_scope = var_env.cur_scope.sub_scopes[0]
        self.scope_var_type_dict = var_env.get_scope_var_type()

    def ast_visit(self):
        self.visit(self.root)
        return self.wrapper_root

    def visit_FunctionDef(self, node):
        self.generic_visit(node)
        if hasattr(node, 'decorator_list'):
            decorator_list = [
                d for d in node.decorator_list if d.id not in DECORATOR_NAMES
            ]
            node.decorator_list = decorator_list
        return node

    def visit_Assign(self, node):
        if self._update_class_node_dict(node):
            return None

        if self._update_name_to_tensor_shape(node):
            return node

        for child_node in gast.walk(node.value):
            if isinstance(child_node, gast.Call):
                self._visit_Call(child_node)
        return node

    def visit_Expr(self, node):
        value_node = node.value
        for child_node in gast.walk(value_node):
            if isinstance(child_node, gast.Call):
                if is_dygraph_api(child_node):
                    return
                else:
                    self._visit_Call(child_node)
        return node

    def visit_Attribute(self, node):
        if self._used_by_paddle_api(node):
            if self.is_tensor_shape(node):
                return create_api_shape_node(node)
        return node

    def visit_Name(self, node):
        if node.id in self.name_to_tensor_shape:
            if self._used_by_paddle_api(node):
                tensor_shape_node = self.name_to_tensor_shape[node.id]
                if isinstance(tensor_shape_node, gast.Attribute):
                    return create_api_shape_node(tensor_shape_node)
                elif isinstance(tensor_shape_node, gast.Subscript):
                    result_node = copy.deepcopy(tensor_shape_node)
                    result_node.value = create_api_shape_node(
                        tensor_shape_node.value)
                    return result_node
        return node

    def _visit_Call(self, node):
        assert isinstance(node, gast.Call)
        # Replace API `to_variable` with `fluid.layers.assign`
        if is_to_variable(node):
            self._update_feed_dict(node)
            node = to_assign_node(node)
            return node

        if is_paddle_api(node):
            # Visit gast.Attribute and gast.Name to replace tensor.shape if necessary
            self.generic_visit(node)

        func_name = astor.to_source(gast.gast_to_ast(node.func))

        if self._is_dygraph_forward(func_name):
            class_node = self._get_class_node(func_name)
            static_node = to_static_ast(node, class_node)
            return static_node
        else:
            return node

    def is_tensor_shape(self, node):
        """
        Return True if node is like `x.shape` and x is Tensor, return False otherwise.
        """
        assert isinstance(node, gast.Attribute)
        if node.attr != 'shape':
            return False

        try:
            value_id = node.value.id
        except AttributeError:
            return False

        if value_id in self.name_to_tensor_shape:
            return True

        # TODO: `value_id` may be not in scope_var_type_dict if `value_id` is the arg of decorated function
        # Need a better way to confirm whether `value_id` is a Tensor.
        try:
            var_type_set = self.scope_var_type_dict[value_id]
        except KeyError:
            return False

        if NodeVarType.NUMPY_NDARRAY in var_type_set:
            return False
        if NodeVarType.TENSOR not in var_type_set and NodeVarType.PADDLE_RETURN_TYPES not in var_type_set:
            return False

        return True

    def _used_by_paddle_api(self, node):
        assert isinstance(node, (gast.Attribute, gast.Name))
        wrapper_node = self.node_to_wrapper_map.get(node)
        if not wrapper_node:
            # Transformed node is not in node_to_wrapper_map
            return False
        while wrapper_node.parent:
            parent_node = wrapper_node.parent.node
            if isinstance(parent_node, gast.Call):
                if is_paddle_api(parent_node):
                    return True
                else:
                    return False
            wrapper_node = wrapper_node.parent

        return False

    def _is_dygraph_forward(self, func_id):
        return func_id in self.class_node_dict

    def _get_class_node(self, func_id):
        return self.class_node_dict[func_id]

    def _update_class_node_dict(self, node):
        assert isinstance(node, gast.Assign)
        node_value = node.value
        if isinstance(node_value, gast.Call):
            if is_to_variable(node_value):
                return False

            if is_dygraph_api(node_value):
                dygraph_api = node_value.func.attr
                if not dygraph_class_to_static_api.get(dygraph_api):
                    return False

                update_args_of_func(node_value, node_value, "__init__")
                target_str = astor.to_source(gast.gast_to_ast(node.targets[0]))
                self.class_node_dict[target_str] = node_value
                return True
            # TODO: node.value is not dygraph class
        return False

    def _update_feed_dict(self, node):
        assert isinstance(node, gast.Call)

        value_node = None
        for kw in node.keywords:
            if kw.arg == 'value':
                value_node = kw.value  # eg: `a` for "value=a "
        if not value_node:
            value_node = node.args[0]

        if not isinstance(value_node, gast.Name):
            return
        else:
            var_name = value_node.id
            feed_var_name = unique_name.generate(var_name)  # eg: "a_0"
            self.feed_name_to_arg_id[
                feed_var_name] = var_name  # eg: "a_0" : "a"

    def get_feed_name_to_arg_id(self):
        return self.feed_name_to_arg_id

    def _update_name_to_tensor_shape(self, node):
        assert isinstance(node, gast.Assign)
        # TODO: Consider node has more than one target. eg: x, y = a, Tensor.shape[1]
        target_node = node.targets[0]
        try:
            target_id = target_node.id
        except AttributeError:
            return False
        value_node = node.value

        if isinstance(value_node, gast.Name):
            if value_node.id in self.name_to_tensor_shape:
                self.name_to_tensor_shape[
                    target_id] = self.name_to_tensor_shape[value_node.id]
                return True
        if isinstance(value_node, gast.Attribute):
            if self.is_tensor_shape(value_node):  # eg: x.shape
                self.name_to_tensor_shape[target_id] = value_node
                return True
        if isinstance(value_node, gast.Subscript):
            if isinstance(value_node.value, gast.Attribute):
                if self.is_tensor_shape(value_node.value):  # eg: x.shape[0]
                    self.name_to_tensor_shape[target_id] = value_node
                    return True
        return False


def convert_to_static(dyfunc):
    """
    Converts dygraph function into static function.
    """
    # Get AST from dygraph function
    raw_code = inspect.getsource(dyfunc)
    code = textwrap.dedent(raw_code)
    root = gast.parse(code)

    # Transform AST
    dygraph_to_static = DygraphToStaticAst()
    root_wrapper = dygraph_to_static.get_static_ast(root)

    # Get static_func from AST
    func_name = dygraph_to_static.get_module_name()
    static_func, file_name = ast_to_func(root_wrapper.node, func_name)
    return static_func, dygraph_to_static
