"""
Extends docspec for the python language, offers facility to resolve names and provides additional informations. 

@see: L{ApiObject.expand_name}
"""

from typing import Iterator, List, Mapping, Optional, Union, Type
import ast
import inspect
import warnings

import attr

from cached_property import cached_property

import docspec

from . import astutils
from .dottedname import DottedName
from .dupsafedict import DuplicateSafeDict

__all__ = [
  'ApiObjectsRoot',
  'Location',
  'Decoration',
  'Argument',
  'ApiObject',
  'Data',
  'Function',
  'Class',
  'Module',
#   'load_module',
#   'load_modules',
#   'dump_module',
#   'filter_visit',
#   'visit',
#   'ReverseMap',
#   'get_member',
]

_RESOLVE_ALIAS_MAX_RECURSE = 5

@attr.s(auto_attribs=True)
class ApiObjectsRoot:
    """
    Root of the tree. Special object that provides a single view on all L{ApiObject}s in the tree and root modules.

    A reference to the root instance is kept on all L{pydocspec} API objects as L{ApiObject.root}.

    @note: L{pydocspec}'s tree contains a hiearchy of packages.
    """

    root_modules: List['Module'] = attr.ib(factory=list, init=False)
    """
    The root modules of the tree.
    """
    
    all_objects: DuplicateSafeDict['ApiObject'] = attr.ib(factory=DuplicateSafeDict, init=False)
    """
    All objects of the tree in a mapping C{full_name} -> L{ApiObject}.
    
    @note: Special care is taken in order no to shadow objects with duplicate names, see L{DuplicateSafeDict}.
    """

class ApiObject(docspec.ApiObject):
    """
    An augmented L{docspec.ApiObject}, with functionalities to resolve names for the python language.
    """

    # help mypy
    parent: Optional[Union['Class', 'Module']] # type: ignore[assignment]

    # this attribute needs to be manually set from the converter/loader.
    _root: ApiObjectsRoot
    
    @property
    def root(self) -> ApiObjectsRoot:
        """
        L{ApiObjectsRoot} instance holding references to all objects in the tree.
        """
        return self._root
    
    @cached_property
    def root_module(self) -> 'Module':
        """
        The root module of this object.
        """
        if isinstance(self, Module) and not self.parent:
            return self
        assert self.parent is not None
        return self.parent.root_module # type:ignore[no-any-return]
    
    # make the location attribute non-optional, reduces annoyance.
    @cached_property
    def location(self) -> docspec.Location:
        return super().location or docspec.Location(filename='<unknown>', lineno=-1)

    @cached_property
    def dotted_name(self) -> DottedName:
        """
        The fully qualified dotted name of this object, as C{DottedName} instance.
        """
        return DottedName(*(ob.name for ob in self.path))

    @cached_property
    def full_name(self) -> str:
        """
        The fully qualified dotted name of this object, as string. 
        This value is used as the key in the L{ApiObject.root.all_objects} dictionnary.
        """
        return str(self.dotted_name)
    
    @cached_property
    def doc_sources(self) -> List['ApiObject']:
        """Objects that can be considered as a source of documentation.

        The motivating example for having multiple sources is looking at a
        superclass' implementation of a method for documentation for a
        subclass'.
        """
        sources = [self]
        if isinstance(self, Inheritable):
            if not isinstance(self.parent, Class):
                return sources
            for b in self.parent.all_base_classes(include_self=False):
                base = b.get_member(self.name)
                if base:
                    sources.append(base)
        return sources
    
    @cached_property
    def module(self) -> 'Module':
        """
        The L{Module} instance that contains this object.
        """
        if isinstance(self, Module):
            return self
        else:
            assert self.parent is not None
            return self.parent.module # type:ignore[no-any-return]
    
    def get_member(self, name: str) -> Optional['ApiObject']:
        """
        Retrieve a member from the API object. This will always return C{None} for
        objects that don't support members (eg. L{Function} and L{Data}).
        """
        if isinstance(self, HasMembers):
            for member in self.members:
                if member.name == name:
                    assert isinstance(member, ApiObject), (name, self, member)
                    return member
        return None
    
    def get_members(self, name: str) -> Iterator['ApiObject']:
        """
        Like C{get_member} but can return several items with the same name.
        """
        if isinstance(self, docspec.HasMembers):
            for member in self.members:
                if member.name == name:
                    assert isinstance(member, ApiObject), (name, self, member)
                    yield member
    
    def expand_name(self, name: str, follow_aliases: bool = True, _indirections: Optional[List['Indirection']]=None) -> str:
        """
        Return a fully qualified name for the possibly-dotted `name`.

        To explain what this means, consider the following modules:
        mod1.py::
            from external_location import External
            class Local:
                pass
        mod2.py::
            from mod1 import External as RenamedExternal
            import mod1 as renamed_mod
            class E:
                pass
        In the context of mod2.E, C{expand_name("RenamedExternal")} should be
        C{"external_location.External"} and C{expand_name("renamed_mod.Local")}
        should be C{"mod1.Local"}. 
        
        This method is in charge to follow the aliases when possible!
        It will reccursively follow any alias entries found 
        up to certain level of complexity. 
        Example:
        mod1.py::
            import external
            class Processor:
                spec = external.Processor.more_spec
            P = Processor
        mod2.py::
            from mod1 import P
            class Runner:
                processor = P
        In the context of mod2, C{expand_name("Runner.processor.spec")} should be
        C{"external.Processor.more_spec"}.
        
        @param name: The name to expand.
        @param follow_aliases: Whether or not to follow aliases. Indirections will still be followed anyway.
        @note: The implementation replies on iterating through the each part of the dotted name, 
            calling L{_local_to_full_name} for each name in their associated context and incrementally building 
            the full_name from that. 
            Lookup members in superclasses when possible and follows aliases and indirections. 
            This mean that L{expand_name} will never return the name of an alias,
            it will always follow it's indirection to the origin. Except if follow_aliases=False. 
        """
        parts = DottedName(name)
        ctx: 'ApiObject' = self # The context for the currently processed part of the name. 
        
        for i, part in enumerate(parts):
            full_name = ctx._local_to_full_name(part, follow_aliases=follow_aliases, _indirections=_indirections)
            if full_name == part and i != 0:
                # The local name was not found.
                # If we're looking at a class, we try our luck with the inherited members
                if isinstance(ctx, Class):
                    f = ctx.find(part)
                    full_name = f.full_name if f else full_name
                # We don't have a full name
                if full_name == part:
                    # TODO: Instead of returning the input, _local_to_full_name
                    #       should probably either return None or raise LookupError.
                    # Or maybe we should find a way to indicate if the expanded name is "guessed" or if we have the the correct full_name. 
                    # With the current implementation, this would mean checking if "parts[i + 1:]" contains anything. 
                    full_name = f'{ctx.full_name}.{part}'
                    break
            nxt = self.root.all_objects.get(full_name)
            if nxt is None:
                break
            ctx = nxt

        return str(DottedName(full_name, *parts[i + 1:]))

    def resolve_name(self, name: str, follow_aliases: bool = True) -> Optional['ApiObject']:
        """
        Return the object named by "name" (using Python's lookup rules) in
        this context, if any is known to this system. 

        @note: This method will never return an L{Indirection} or an alias since it's supposed to follow 
            indirections and aliases. Except if follow_aliases=False. 
        """
        return self.root.all_objects.get(self.expand_name(name, follow_aliases=follow_aliases))

    def _local_to_full_name(self, name: str, follow_aliases: bool, _indirections: Optional[List['Indirection']]=None) -> str:
        if not isinstance(self, HasMembers):
            assert self.parent is not None
            return self.parent._local_to_full_name(name, follow_aliases, _indirections)
        
        # Follows indirections and aliases
        member = self.get_member(name)
        if member:
            if follow_aliases and isinstance(member, Data) and member.is_alias:
                return self._resolve_indirection(member._alias_indirection, _indirections) or member.full_name
            if isinstance(member, Indirection):
                return self._resolve_indirection(member, _indirections) or member.full_name
            return member.full_name # type:ignore[no-any-return]

        elif isinstance(self, Class):
            assert self.parent is not None
            return self.parent._local_to_full_name(name, follow_aliases, _indirections)
        
        return name
    
    def _resolve_indirection(self, indirection: 'Indirection', _indirections: Optional[List['Indirection']]=None) -> Optional[str]:
        """
        If the object is an alias or an indirection, then follow it and return the supposed full name fo the origin object,
        or return the passed object's full name.

        Resolve the alias value to it's target full name.
        Or fall back to original alias full name if we know we've exhausted the max recursions.

        @param alias: an ALIAS object.
        @param indirections: Chain of alias objects followed. 
            This variable is used to prevent infinite loops when doing the lookup.
        @note: It can return None in exceptionnal cases if an indirection cannot be resolved. 
            Then we use the indirection's full_name. 
        """

        if _indirections and len(_indirections) > _RESOLVE_ALIAS_MAX_RECURSE:
            return _indirections[0].full_name # type:ignore[no-any-return]

        target = indirection.target
        
        # the context is important
        ctx = indirection.parent
        assert ctx is not None

        # This checks avoids infinite recursion error when a indirection has the same name as it's value
        if _indirections and _indirections[-1] != indirection or not _indirections:
            # We redirect to the original object instead!
            return ctx.expand_name(target, _indirections=(_indirections or [])+[indirection])
        else:
            # Issue tracing the alias back to it's original location, found the same indirection again.
            if ctx.parent is not None and ctx.module == ctx.parent.module:
                # We try with the parent scope, only if the parent is in the same module, otherwise fail. 
                # This is used in situations like in the pydoctor.model.System class and it's aliases, 
                # because they have the same target name as the name they are aliasing, it's causing trouble.
                return ctx.parent.expand_name(target, _indirections=(_indirections or [])+[indirection])
        
        return None

    def _warns(self, msg: str) -> None:
        warnings.warn(f'{self.full_name}:{self.location.linenumber} - {msg}')

class Data(docspec.Data, ApiObject):
    """
    Represents a variable assignment.
    """
    parent: Union['Class', 'Module']

    @cached_property
    def datatype_ast(self) -> Optional[ast.expr]:
        """
        The AST expresssion of the annotation of this data.
        """
        if self.datatype:
            return astutils.unstring_annotation(
                    astutils.extract_expr(self.datatype, filename=self.location.filename), self)
        return None

    @cached_property
    def value_ast(self) -> Optional[ast.expr]:
        """
        The AST expresssion of the value assigned to this Data.
        """
        if self.value:
            return astutils.extract_expr(self.value, filename=self.location.filename)
        return None

    @cached_property
    def is_instance_variable(self) -> bool:
        """
        Whether this Data is an instance variable.
        """
        ...
        # TODO: Think about how to differenciate beetwen instance and class variables ?
    @cached_property
    def is_class_variable(self) -> bool:
        """
        Whether this Data is a class variable.
        """
        ...

    @cached_property
    def is_attrs_attribute(self) -> bool:
        """
        Whether this Data is an L{attr.ib} attribute.
        """
        return isinstance(self.value_ast, ast.Call) and \
            astutils.node2fullname(self.value_ast.func, self) in (
                'attr.ib', 'attr.attrib', 'attr.attr'
                )
    
    @cached_property
    def is_dataclass_field(self) -> bool:
        """
        Whether this Data is a L{dataclasses.field} attribute.
        """
        return isinstance(self.value_ast, ast.Call) and \
            astutils.node2fullname(self.value_ast.func, self) in (
                'dataclasses.field',
                )
    
    @cached_property
    def is_alias(self) -> bool:
        """
        Whether this Data is an alias.
        Aliases are folowed by default when using L{ApiObject.expand_name}. 
        """
        return astutils.node2dottedname(self.value_ast) is not None
    
    @cached_property
    def _alias_indirection(self) -> 'Indirection':
        assert self.is_alias
        assert self.value is not None
        indirection = Indirection(self.name, self.location, None, self.value)
        indirection.parent = self.parent
        indirection._root = self.root
        return indirection
    
    @cached_property
    def is_constant(self) -> bool:
        """
        Whether this Data is a constant. 
        
        This checks two things:
            - all-caps variable name
            - typing.Final annotation
        """
        return self.name.isupper() or self.is_using_typing_final
    
    @cached_property
    def is_using_typing_final(self) -> bool:
        """
        Detect if this object is using L{typing.Final} as annotation.
        """
        full_name = astutils.node2fullname(self.datatype_ast, self)
        if full_name == "typing.Final":
            return True
        if isinstance(self.datatype_ast, ast.Subscript):
            # Final[...] or typing.Final[...] expressions
            if isinstance(self.datatype_ast.value, (ast.Name, ast.Attribute)):
                value = self.datatype_ast.value
                full_name = astutils.node2fullname(value, self)
                if full_name == "typing.Final":
                    return True

        return False

class Indirection(docspec.Indirection, ApiObject):
  """
  Represents an imported name. It can be used to properly 
  find the full name target of a link written with a local name. 
  """

class Class(docspec.Class, ApiObject):
    """
    Represents a class definition.
    """
    # help mypy
    decorations: Optional[List['Decoration']] # type:ignore[assignment]
    parent: Union['Class', 'Module']
    members: List['ApiObject'] # type:ignore[assignment]

    @cached_property
    def resolved_bases(self) -> List[Union['ApiObject', 'str']]:
        """
        For each bases, try to resolve the name to an L{ApiObject} or fallback to the expanded name.
        
        @see: `resolve_name` and `expand_name`
        """
        objs = []
        for base in self.bases or ():
            objs.append(self.parent.resolve_name(base) or self.parent.expand_name(base))
        return objs
    
    def all_bases(self, include_self: bool = False) -> Iterator[Union['ApiObject', 'str']]:
        """Reccursively returns resolved_bases for all bases."""
        if include_self:
            yield self
        for b in self.resolved_bases:
            if isinstance(b, Class):
                yield from b.all_bases(True)
            else:
                yield b

    def all_base_classes(self, include_self: bool = False) -> Iterator['Class']:
        """Reccursively returns all bases that are resolved to a Class."""
        for b in self.all_bases(include_self):
            if isinstance(b, Class):
                yield b

    def find(self, name: str) -> Optional[ApiObject]:
        """Look up a name in this class and its base classes.

        @return: the object with the given name, or L{None} if there isn't one
        @note: This does not currently comply with the python method resolution 
            order. We would need to implement C3Linearization algorithm with Class objects. 
        """
        for base in self.all_base_classes(True):
            obj: Optional['ApiObject'] = base.get_member(name)
            if obj is not None:
                return obj
        return None
    
    @cached_property
    def constructor_params(self) -> Mapping[str, Optional[ast.expr]]:
        """
        A mapping of constructor parameter names to their type annotation.
        If a parameter is not annotated, its value is L{None}.

        @note: The implementation currently relies on inspecting the C{__init__} method only.
            If C{__new__} or L{__call__} methods are defined, this information might be incorrect.
        """
        init_method = self.get_member('__init__')
        if isinstance(init_method, Function):
            args = {}
            for arg in init_method.args:
                args[arg.name] = arg.datatype_ast
            return args
        else:
            return {'self': None}
    
    # List of exceptions class names in the standard library, Python 3.8.10
    _exceptions = ('ArithmeticError', 'AssertionError', 'AttributeError', 
        'BaseException', 'BlockingIOError', 'BrokenPipeError', 
        'BufferError', 'BytesWarning', 'ChildProcessError', 
        'ConnectionAbortedError', 'ConnectionError', 
        'ConnectionRefusedError', 'ConnectionResetError', 
        'DeprecationWarning', 'EOFError', 
        'EnvironmentError', 'Exception', 'FileExistsError', 
        'FileNotFoundError', 'FloatingPointError', 'FutureWarning', 
        'GeneratorExit', 'IOError', 'ImportError', 'ImportWarning', 
        'IndentationError', 'IndexError', 'InterruptedError', 
        'IsADirectoryError', 'KeyError', 'KeyboardInterrupt', 'LookupError', 
        'MemoryError', 'ModuleNotFoundError', 'NameError', 
        'NotADirectoryError', 'NotImplementedError', 
        'OSError', 'OverflowError', 'PendingDeprecationWarning', 'PermissionError', 
        'ProcessLookupError', 'RecursionError', 'ReferenceError', 
        'ResourceWarning', 'RuntimeError', 'RuntimeWarning', 'StopAsyncIteration', 
        'StopIteration', 'SyntaxError', 'SyntaxWarning', 'SystemError', 
        'SystemExit', 'TabError', 'TimeoutError', 'TypeError', 
        'UnboundLocalError', 'UnicodeDecodeError', 'UnicodeEncodeError', 
        'UnicodeError', 'UnicodeTranslateError', 'UnicodeWarning', 'UserWarning', 
        'ValueError', 'Warning', 'ZeroDivisionError')
    @cached_property
    def is_exception(self) -> bool:
        """Return C{True} if this class extends one of the standard library exceptions."""
        
        for base in self.all_bases(True):
            if base in self._exceptions:
                return True
        return False
    
    @cached_property
    def dataclass_decoration(self) -> Optional['Decoration']:
        """The L{dataclass} decoration of this class, if any."""
        for deco in self.decorations or ():
            if astutils.node2fullname(deco.name_ast, self.parent) in ('dataclasses.dataclass',):
                return deco
        return None

    @cached_property
    def attrs_decoration(self) -> Optional['Decoration']:
        """The L{attr.s} decoration of this class, if any."""
        for deco in self.decorations or ():
            if astutils.node2fullname(deco.name_ast, self.parent) in ('attr.s', 'attr.attrs', 'attr.attributes'):
                return deco
        return None

    @cached_property
    def uses_attrs_auto_attribs(self) -> bool:
        """Does the C{attr.s()} decoration contain C{auto_attribs=True}?"""
        attrs_deco = self.attrs_decoration
        if attrs_deco is not None and isinstance(attrs_deco.expr_ast, ast.Call):
            return astutils.uses_auto_attribs(attrs_deco.expr_ast, self)
        return False

class Function(docspec.Function, ApiObject):
    """
    Represents a function definition.
    """
    # help mypy
    decorations: Optional[List['Decoration']] # type:ignore
    args: List['Argument'] # type:ignore
    parent: Union[Class, 'Module']

    @cached_property
    def return_type_ast(self) -> Optional[ast.expr]:
        if self.return_type:
            return astutils.unstring_annotation(
                    astutils.extract_expr(self.return_type, filename=self.location.filename), self)
        return None

    @cached_property
    def is_property(self) -> bool:
        for deco in self.decorations or ():
            name = astutils.node2fullname(deco.name_ast, self.parent)
            if name and name.endswith(('property', 'Property')):
                return True
        return False
    
    @cached_property
    def is_property_setter(self) -> bool:
        for deco in self.decorations or ():
            name = astutils.node2dottedname(deco.name_ast)
            if name and len(name) == 2 and name[0]==self.name and name[1] == 'setter':
                return True
        return False
    
    @cached_property
    def is_property_deleter(self) -> bool:
        for deco in self.decorations or ():
            name = astutils.node2dottedname(deco.name_ast)
            if name and len(name) == 2 and name[0]==self.name and name[1] == 'deleter':
                return True
        return False
    
    @cached_property
    def is_async(self) -> bool:
        return 'async' in (self.modifiers or ())
    
    @cached_property
    def is_method(self) -> bool:
        return isinstance(self.parent, Class)
    
    @cached_property
    def is_classmethod(self) -> bool:
        for deco in self.decorations or ():
            if astutils.node2fullname(deco.name_ast, self.parent) == 'classmethod':
                return True
        return False
    
    @cached_property
    def is_staticmethod(self) -> bool:
        for deco in self.decorations or ():
            if astutils.node2fullname(deco.name_ast, self.parent) == 'staticmethod':
                return True
        return False
    
    @cached_property
    def is_abstractmethod(self) -> bool:
        for deco in self.decorations or ():
            if astutils.node2fullname(deco.name_ast, self.parent) in ['abc.abstractmethod', 'abc.abstractproperty']:
                return True
        return False
    
    def signature(self, include_types:bool=True, include_defaults:bool=True, 
                  include_return_type:bool=True, include_self:bool=True,
                  signature_class: Type[inspect.Signature] = inspect.Signature, 
                  value_formatter_calss: Type[astutils.ValueFormatter] = astutils.ValueFormatter) -> inspect.Signature:
        # TODO: copy the SignatureBuilder and related code here.
        return inspect.Signature()

class Argument(docspec.Argument):
    """
    Represents a L{Function} argument.
    """
    @cached_property
    def datatype_ast(self) -> Optional[ast.expr]:
        if self.datatype:
            return astutils.unstring_annotation(
                    astutils.extract_expr(self.datatype)) # TODO find a way to report warnings correctly even if Argument is not an ApiObject.
        return None

    @cached_property
    def default_value_ast(self) -> Optional[ast.expr]:
        if self.default_value:
            return astutils.extract_expr(self.default_value)
        return None

class Decoration(docspec.Decoration):
    """
    Represents a decorator on a L{Class} or L{Function}.
    """
    @cached_property
    def name_ast(self) -> ast.expr:
        return astutils.extract_expr(self.name)

    @cached_property
    def expr_ast(self) -> ast.expr:
        return astutils.extract_expr(self.name + (self.args or ''))

class Module(docspec.Module, ApiObject):
    """
    Represents a module, basically a named container for code/API objects. Modules may be nested in other modules
    """

    members: List['ApiObject'] # type:ignore[assignment]

    @cached_property
    def is_package(self) -> bool:
        """
        @note: Currently, packages without submodules will be considered as regular modules.
        """
        return any(isinstance(o, docspec.Module) for o in self.members)

    @cached_property
    def all(self) -> Optional[List[str]]:
        """Parse the module variable __all__ into a list of names."""

        var = self.get_member('__all__')
        if not var or not isinstance(var, Data):
            return None
        value = var.value_ast

        if not isinstance(value, (ast.List, ast.Tuple)):
            self._warns('Cannot parse value assigned to "__all__", must be a list or tuple.')
            return None

        names = []
        for idx, item in enumerate(value.elts):
            try:
                name: object = ast.literal_eval(item)
            except ValueError:
                self._warns(f'Cannot parse element {idx} of "__all__"')
            else:
                if isinstance(name, str):
                    names.append(name)
                else:
                    self._warns(f'Element {idx} of "__all__" has '
                        f'type "{type(name).__name__}", expected "str"')

        return names
    
    @cached_property
    def docformat(self) -> Optional[str]:
        """
        Parses module's __docformat__ variable.
        """
        var = self.get_member('__all__')
        if not var or not isinstance(var, Data):
            return None

        try:
            value = ast.literal_eval(var.value_ast)
        except ValueError:
            var._warns('Cannot parse value assigned to "__docformat__": not a string')
            return None
        
        if not isinstance(value, str):
            var._warns('Cannot parse value assigned to "__docformat__": not a string')
            return None
            
        if not value.strip():
            var._warns('Cannot parse value assigned to "__docformat__": empty value')
            return None
        
        return value

class zopedocspec:
    ...

Location = docspec.Location

HasMembers = (Module, Class)
"""
Alias to use with C{isinstance()}
"""

Inheritable = (Indirection, Data, Function) 
"""
Alias to use with C{isinstance()}
"""
