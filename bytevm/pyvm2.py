"""A pure-Python Python bytecode interpreter."""
# Based on:
# pyvm2 by Paul Swartz (z3p), from http://www.twistedmatrix.com/users/z3p/

from __future__ import print_function, division
import dis
import inspect
import linecache
import logging
import operator
import sys
import types
from .sys import pseudosys

import os.path
import imp
NoSource = Exception
Loaded = {}
Intercept_Imports = True
Interpret_Original = True

import six
from six.moves import reprlib

PY3, PY2 = six.PY3, not six.PY3

from .pyobj import Frame, Block, Method, Function, Generator, Cell, traceback

log = logging.getLogger(__name__)

import pudb
brk = pudb.set_trace

if six.PY3:
    byteint = lambda b: b
else:
    byteint = ord

# Create a repr that won't overflow.
repr_obj = reprlib.Repr()
repr_obj.maxother = 120
repper = repr_obj.repr


class VirtualMachineError(Exception):
    """For raising errors in the operation of the VM."""
    pass


class VirtualMachine(object):
    steps = 0
    def __init__(self):
        # the number of steps this VM executed
        # The call stack of frames.
        self.frames = []
        # The current frame.
        self.frame = None
        self.return_value = None
        self.last_exception = None

    def _i(self):
        return (VirtualMachine.steps, self.frame.stack if self.frame else None)

    def top(self):
        """Return the value at the top of the stack, with no changes."""
        return self.frame.stack[-1]

    def pop(self, i=0):
        """Pop a value from the stack.

        Default to the top of the stack, but `i` can be a count from the top
        instead.

        """
        return self.frame.stack.pop(-1-i)

    def push(self, *vals):
        """Push values onto the value stack."""
        self.frame.stack.extend(vals)

    def popn(self, n):
        """Pop a number of values from the value stack.

        A list of `n` values is returned, the deepest value first.

        """
        if n:
            ret = self.frame.stack[-n:]
            self.frame.stack[-n:] = []
            return ret
        else:
            return []

    def peek(self, n):
        """Get a value `n` entries down in the stack, without changing the stack."""
        return self.frame.stack[-n]

    def jump(self, jump):
        """Move the bytecode pointer to `jump`, so it will execute next."""
        self.frame.f_lasti = jump

    def push_block(self, type, handler=None, level=None):
        if level is None:
            level = len(self.frame.stack)
        self.frame.block_stack.append(Block(type, handler, level))

    def pop_block(self):
        return self.frame.block_stack.pop()

    def make_frame(self, code, callargs={}, f_globals=None, f_locals=None, f_closure=None):
        log.info("make_frame: code=%r, callargs=%s" % (code, repper(callargs)))
        if f_globals is not None:
            f_globals = f_globals
            if f_locals is None:
                f_locals = f_globals
        elif self.frames:
            f_globals = self.frame.f_globals
            f_locals = {}
        else:
            f_globals = f_locals = {
                '__builtins__': __builtins__,
                '__name__': '__main__',
                '__doc__': None,
                '__package__': None,
            }
        f_locals.update(callargs)
        frame = Frame(code, f_globals, f_locals, f_closure, self.frame)
        return frame

    def push_frame(self, frame):
        self.frames.append(frame)
        self.frame = frame

    def pop_frame(self):
        self.frames.pop()
        if self.frames:
            self.frame = self.frames[-1]
        else:
            self.frame = None

    def print_frames(self):
        """Print the call stack, for debugging."""
        for f in self.frames:
            filename = f.f_code.co_filename
            lineno = f.line_number()
            print('  File "%s", line %d, in %s' % (
                filename, lineno, f.f_code.co_name
            ))
            linecache.checkcache(filename)
            line = linecache.getline(filename, lineno, f.f_globals)
            if line:
                print('    ' + line.strip())

    def resume_frame(self, frame):
        frame.f_back = self.frame
        val = self.run_frame(frame)
        frame.f_back = None
        return val

    def run_code(self, code, f_globals=None, f_locals=None):
        frame = self.make_frame(code, f_globals=f_globals, f_locals=f_locals)
        val = self.run_frame(frame)
        # Check some invariants
        if self.frames:            # pragma: no cover
            raise VirtualMachineError("Frames left over!")
        if self.frame and self.frame.stack:             # pragma: no cover
            raise VirtualMachineError("Data left on stack! %r" % self.frame.stack)

        return val

    def unwind_block(self, block):
        if block.type == 'except-handler':
            offset = 3
        else:
            offset = 0

        while len(self.frame.stack) > block.level + offset:
            self.pop()

        if block.type == 'except-handler':
            tb, value, exctype = self.popn(3)
            self.last_exception = exctype, value, tb

    def f(self, frame):
        return "%s:%s (%s)" % (frame.f_code.co_filename, frame.line_number(), frame.f_code.co_name)

    def w(self):
        return self.f(self.frame)

    def ww(self):
        fstr = []
        for f in reversed(self.frames):
            fstr.append(self.f(f))
        return ', '.join(fstr)


    def parse_byte_and_args(self):
        """ Parse 1 - 3 bytes of bytecode into
        an instruction and optionally arguments.
        In Python3.6 the format is 2 bytes per instruction."""
        f = self.frame
        self.fn = f.f_code.co_filename
        self.cn = f.f_code.co_name
        opoffset = f.f_lasti
        if sys.version_info >= (3, 6):
            currentOp = f.opcodes[opoffset]
            if currentOp.starts_line:
                f._line = currentOp.starts_line

            byteCode = currentOp.opcode
            byteName = currentOp.opname
        else:
            byteCode = byteint(f.f_code.co_code[opoffset])
            byteName = dis.opname[byteCode]
        f.f_lasti += 1
        arg = None
        arguments = []
        if sys.version_info >= (3, 6) and byteCode == dis.EXTENDED_ARG:
            # Prefixes any opcode which has an argument too big to fit into the
            # default two bytes. ext holds two additional bytes which, taken
            # together with the subsequent opcode’s argument, comprise a
            # four-byte argument, ext being the two most-significant bytes.
            # We simply ignore the EXTENDED_ARG because that calculation
            # is already done by dis, and stored in next currentOp.
            # Lib/dis.py:_unpack_opargs
            return self.parse_byte_and_args()
        if byteCode >= dis.HAVE_ARGUMENT:
            if sys.version_info >= (3, 6):
                intArg = currentOp.arg
            else:
                arg = f.f_code.co_code[f.f_lasti:f.f_lasti+2]
                f.f_lasti += 2
                intArg = byteint(arg[0]) + (byteint(arg[1]) << 8)
            if byteCode in dis.hasconst:
                arg = f.f_code.co_consts[intArg]
            elif byteCode in dis.hasfree:
                if intArg < len(f.f_code.co_cellvars):
                    arg = f.f_code.co_cellvars[intArg]
                else:
                    var_idx = intArg - len(f.f_code.co_cellvars)
                    arg = f.f_code.co_freevars[var_idx]
            elif byteCode in dis.hasname:
                arg = f.f_code.co_names[intArg]
            elif byteCode in dis.hasjrel:
                if sys.version_info >= (3, 6):
                    arg = f.f_lasti + intArg//2
                else:
                    arg = f.f_lasti + intArg
            elif byteCode in dis.hasjabs:
                if sys.version_info >= (3, 6):
                    arg = intArg//2
                else:
                    arg = intArg
            elif byteCode in dis.haslocal:
                arg = f.f_code.co_varnames[intArg]
            else:
                arg = intArg
            arguments = [arg]

        return byteName, arguments, opoffset

    def log(self, byteName, arguments, opoffset):
        """ Log arguments, block stack, and data stack for each opcode."""
        op = "%d: %s" % (opoffset, byteName)
        if arguments:
            op += " %r" % (arguments[0],)
        indent = "    "*(len(self.frames)-1)
        stack_rep = repper(self.frame.stack)
        block_stack_rep = repper(self.frame.block_stack)

        log.info("  %sdata: %s" % (indent, stack_rep))
        log.info("  %sblks: %s" % (indent, block_stack_rep))
        log.info("%s<%s>%s" % (indent, self.steps, op))

    def dispatch(self, byteName, arguments):
        """ Dispatch by bytename to the corresponding methods.
        Exceptions are caught and set on the virtual machine."""
        why = None
        try:
            if byteName.startswith('UNARY_'):
                self.unaryOperator(byteName[6:])
            elif byteName.startswith('BINARY_'):
                self.binaryOperator(byteName[7:])
            elif byteName.startswith('INPLACE_'):
                self.inplaceOperator(byteName[8:])
            elif 'SLICE+' in byteName:
                self.sliceOperator(byteName)
            else:
                # dispatch
                bytecode_fn = getattr(self, 'byte_%s' % byteName, None)
                if not bytecode_fn:            # pragma: no cover
                    raise VirtualMachineError(
                        "unknown bytecode type: %s" % byteName
                    )
                why = bytecode_fn(*arguments)

        except:
            # deal with exceptions encountered while executing the op.
            self.last_exception = sys.exc_info()[:2] + (None,)
            #log.exception("Caught exception during execution")
            why = 'exception'

        return why

    def manage_block_stack(self, why):
        """ Manage a frame's block stack.
        Manipulate the block stack and data stack for looping,
        exception handling, or returning."""
        assert why != 'yield'

        block = self.frame.block_stack[-1]
        if block.type == 'loop' and why == 'continue':
            self.jump(self.return_value)
            why = None
            return why

        self.pop_block()
        self.unwind_block(block)

        if block.type == 'loop' and why == 'break':
            why = None
            self.jump(block.handler)
            return why

        if PY2:
            if (
                block.type == 'finally' or
                (block.type == 'setup-except' and why == 'exception') or
                block.type == 'with'
            ):
                if why == 'exception':
                    exctype, value, tb = self.last_exception
                    self.push(tb, value, exctype)
                else:
                    if why in ('return', 'continue'):
                        self.push(self.return_value)
                    self.push(why)

                why = None
                self.jump(block.handler)
                return why

        elif PY3:
            if (
                why == 'exception' and
                block.type in ['setup-except', 'finally']
            ):
                self.push_block('except-handler')
                exctype, value, tb = self.last_exception
                self.push(tb, value, exctype)
                # PyErr_Normalize_Exception goes here
                self.push(tb, value, exctype)
                why = None
                self.jump(block.handler)
                return why

            elif block.type == 'finally':
                if why in ('return', 'continue'):
                    self.push(self.return_value)
                self.push(why)

                why = None
                self.jump(block.handler)
                return why

        return why


    def run_frame(self, frame):
        """Run a frame until it returns (somehow).

        Exceptions are raised, the return value is returned.

        """
        self.push_frame(frame)
        while True:
            VirtualMachine.steps += 1
            byteName, arguments, opoffset = self.parse_byte_and_args()
            if log.isEnabledFor(logging.INFO):
                self.log(byteName, arguments, opoffset)

            # When unwinding the block stack, we need to keep track of why we
            # are doing it.
            why = self.dispatch(byteName, arguments)
            if why == 'exception':
                # TODO: ceval calls PyTraceBack_Here, not sure what that does.
                pass

            if why == 'reraise':
                why = 'exception'

            if why != 'yield':
                while why and frame.block_stack:
                    # Deal with any block management we need to do.
                    why = self.manage_block_stack(why)

            if why:
                break

        # TODO: handle generator exception state

        self.pop_frame()

        if why == 'exception':
            if self.last_exception:
                et, val, tb = self.last_exception
                raise val
            else:
                raise Exception('%s %s %s' % (byteName, arguments, opoffset))

        return self.return_value

    ## Stack manipulation

    def byte_LOAD_CONST(self, const):
        self.push(const)

    def byte_POP_TOP(self):
        self.pop()

    def byte_DUP_TOP(self):
        self.push(self.top())

    def byte_DUP_TOPX(self, count):
        items = self.popn(count)
        for i in [1, 2]:
            self.push(*items)

    def byte_DUP_TOP_TWO(self):
        # Py3 only
        a, b = self.popn(2)
        self.push(a, b, a, b)

    def byte_ROT_TWO(self):
        a, b = self.popn(2)
        self.push(b, a)

    def byte_ROT_THREE(self):
        a, b, c = self.popn(3)
        self.push(c, a, b)

    def byte_ROT_FOUR(self):
        a, b, c, d = self.popn(4)
        self.push(d, a, b, c)

    ## Names

    def byte_LOAD_NAME(self, name):
        frame = self.frame
        if name in frame.f_locals:
            val = frame.f_locals[name]
        elif name in frame.f_globals:
            val = frame.f_globals[name]
        elif name in frame.f_builtins:
            val = frame.f_builtins[name]
        else:
            raise NameError("name '%s' is not defined" % name)
        self.push(val)

    def byte_STORE_NAME(self, name):
        self.frame.f_locals[name] = self.pop()

    def byte_DELETE_NAME(self, name):
        del self.frame.f_locals[name]

    def byte_LOAD_FAST(self, name):
        if name in self.frame.f_locals:
            val = self.frame.f_locals[name]
        else:
            raise UnboundLocalError(
                "local variable '%s' referenced before assignment" % name
            )
        self.push(val)

    def byte_STORE_FAST(self, name):
        self.frame.f_locals[name] = self.pop()

    def byte_DELETE_FAST(self, name):
        del self.frame.f_locals[name]

    def byte_LOAD_GLOBAL(self, name):
        f = self.frame
        if name in f.f_globals:
            val = f.f_globals[name]
        elif name in f.f_builtins:
            val = f.f_builtins[name]
        else:
            if PY2:
                raise NameError("global name '%s' is not defined" % name)
            elif PY3:
                raise NameError("name '%s' is not defined" % name)
        self.push(val)

    def byte_STORE_GLOBAL(self, name):
        f = self.frame
        f.f_globals[name] = self.pop()

    def byte_LOAD_DEREF(self, name):
        self.push(self.frame.cells[name].get())

    def byte_STORE_DEREF(self, name):
        self.frame.cells[name].set(self.pop())

    def byte_LOAD_LOCALS(self):
        self.push(self.frame.f_locals)

    ## Operators

    UNARY_OPERATORS = {
        'POSITIVE': operator.pos,
        'NEGATIVE': operator.neg,
        'NOT':      operator.not_,
        'CONVERT':  repr,
        'INVERT':   operator.invert,
    }

    def unaryOperator(self, op):
        x = self.pop()
        self.push(self.UNARY_OPERATORS[op](x))

    BINARY_OPERATORS = {
        'POWER':    pow,
        'MULTIPLY': operator.mul,
        'DIVIDE':   getattr(operator, 'div', lambda x, y: None),
        'FLOOR_DIVIDE': operator.floordiv,
        'TRUE_DIVIDE':  operator.truediv,
        'MODULO':   operator.mod,
        'ADD':      operator.add,
        'SUBTRACT': operator.sub,
        'SUBSCR':   operator.getitem,
        'LSHIFT':   operator.lshift,
        'RSHIFT':   operator.rshift,
        'AND':      operator.and_,
        'XOR':      operator.xor,
        'OR':       operator.or_,
    }

    def binaryOperator(self, op):
        x, y = self.popn(2)
        self.push(self.BINARY_OPERATORS[op](x, y))

    def inplaceOperator(self, op):
        x, y = self.popn(2)
        if op == 'POWER':
            x **= y
        elif op == 'MULTIPLY':
            x *= y
        elif op in ['DIVIDE', 'FLOOR_DIVIDE']:
            x //= y
        elif op == 'TRUE_DIVIDE':
            x /= y
        elif op == 'MODULO':
            x %= y
        elif op == 'ADD':
            x += y
        elif op == 'SUBTRACT':
            x -= y
        elif op == 'LSHIFT':
            x <<= y
        elif op == 'RSHIFT':
            x >>= y
        elif op == 'AND':
            x &= y
        elif op == 'XOR':
            x ^= y
        elif op == 'OR':
            x |= y
        else:           # pragma: no cover
            raise VirtualMachineError("Unknown in-place operator: %r" % op)
        self.push(x)

    def sliceOperator(self, op):
        start = 0
        end = None          # we will take this to mean end
        op, count = op[:-2], int(op[-1])
        if count == 1:
            start = self.pop()
        elif count == 2:
            end = self.pop()
        elif count == 3:
            end = self.pop()
            start = self.pop()
        l = self.pop()
        if end is None:
            end = len(l)
        if op.startswith('STORE_'):
            l[start:end] = self.pop()
        elif op.startswith('DELETE_'):
            del l[start:end]
        else:
            self.push(l[start:end])

    COMPARE_OPERATORS = [
        operator.lt,
        operator.le,
        operator.eq,
        operator.ne,
        operator.gt,
        operator.ge,
        lambda x, y: x in y,
        lambda x, y: x not in y,
        lambda x, y: x is y,
        lambda x, y: x is not y,
        lambda x, y: issubclass(x, Exception) and issubclass(x, y),
    ]

    def byte_COMPARE_OP(self, opnum):
        x, y = self.popn(2)
        self.push(self.COMPARE_OPERATORS[opnum](x, y))

    ## Attributes and indexing

    def byte_LOAD_ATTR(self, attr):
        obj = self.pop()
        if type(obj) is Function and attr == '__qualname__':
            val = getattr(obj, '__qname__')
        else:
            val = getattr(obj, attr)
        self.push(val)

    def byte_STORE_ATTR(self, name):
        val, obj = self.popn(2)
        setattr(obj, name, val)

    def byte_DELETE_ATTR(self, name):
        obj = self.pop()
        delattr(obj, name)

    def byte_STORE_SUBSCR(self):
        val, obj, subscr = self.popn(3)
        obj[subscr] = val

    def byte_DELETE_SUBSCR(self):
        obj, subscr = self.popn(2)
        del obj[subscr]

    def byte_GET_AWAITABLE(self):
        # Implements TOS = get_awaitable(TOS), where get_awaitable(o) returns
        # o if o is a coroutine object or a generator object with the
        # CO_ITERABLE_COROUTINE flag, or resolves o.__await__.
        # new from 3.5
        tos = self.top()
        if isinstance(tos, types.GeneratorType) or isinstance(tos, types.CoroutineType):
            return
        tos = self.pop()
        self.push(tos.__await__())

    ## Building

    def byte_BUILD_TUPLE_UNPACK_WITH_CALL(self, count):
        # This is similar to BUILD_TUPLE_UNPACK, but is used for f(*x, *y, *z)
        # call syntax. The stack item at position count + 1 should be the
        # corresponding callable f.
        self.build_container_flat(count, tuple)

    def byte_BUILD_TUPLE_UNPACK(self, count):
        # Pops count iterables from the stack, joins them in a single tuple,
        # and pushes the result. Implements iterable unpacking in
        # tuple displays (*x, *y, *z).
        self.build_container_flat(count, tuple)

    def byte_BUILD_TUPLE(self, count):
        self.build_container(count, tuple)


    def byte_BUILD_LIST_UNPACK(self, count):
        # This is similar to BUILD_TUPLE_UNPACK, but a list instead of tuple.
        # Implements iterable unpacking in list displays [*x, *y, *z].
        self.build_container_flat(count, list)

    def byte_BUILD_SET_UNPACK(self, count):
        # This is similar to BUILD_TUPLE_UNPACK, but a set instead of tuple.
        # Implements iterable unpacking in set displays {*x, *y, *z}.
        self.build_container_flat(count, set)

    def byte_BUILD_MAP_UNPACK_WITH_CALL(self, count):
        # Pops count mappings from the stack, merges them to a single dict,
        # and pushes the result. Implements dictionary unpacking in dictionary
        # displays {**x, **y, **z}.
        self.byte_BUILD_MAP_UNPACK(count)

    def byte_BUILD_MAP_UNPACK(self, count):
        elts = self.popn(count)
        d = {}
        for i in elts:
            d.update(i)
        self.push(d)

    def build_container_flat(self, count, container_fn) :
        elts = self.popn(count)
        self.push(container_fn(e for l in elts for e in l))

    def build_container(self, count, container_fn) :
        elts = self.popn(count)
        self.push(container_fn(elts))

    def byte_BUILD_LIST(self, count):
        elts = self.popn(count)
        self.push(elts)

    def byte_BUILD_SET(self, count):
        # TODO: Not documented in Py2 docs.
        elts = self.popn(count)
        self.push(set(elts))

    def byte_BUILD_CONST_KEY_MAP(self, count):
        # count values are consumed from the stack.
        # The top element contains tuple of keys
        # added in version 3.6
        keys = self.pop()
        values = self.popn(count)
        kvs = dict(zip(keys, values))
        self.push(kvs)

    def byte_BUILD_MAP(self, count):
        # Pushes a new dictionary on to stack.
        if sys.version_info < (3, 5):
            self.push({})
            return
        # Pop 2*count items so that
        # dictionary holds count entries: {..., TOS3: TOS2, TOS1:TOS}
        # updated in version 3.5
        kvs = {}
        for i in range(0, count):
            key, val = self.popn(2)
            kvs[key] = val
        self.push(kvs)

    def byte_STORE_MAP(self):
        the_map, val, key = self.popn(3)
        the_map[key] = val
        self.push(the_map)

    def byte_UNPACK_SEQUENCE(self, count):
        seq = self.pop()
        for x in reversed(list(seq)):
            self.push(x)

    def byte_BUILD_SLICE(self, count):
        if count == 2:
            x, y = self.popn(2)
            self.push(slice(x, y))
        elif count == 3:
            x, y, z = self.popn(3)
            self.push(slice(x, y, z))
        else:           # pragma: no cover
            raise VirtualMachineError("Strange BUILD_SLICE count: %r" % count)

    def byte_LIST_APPEND(self, count):
        val = self.pop()
        the_list = self.peek(count)
        the_list.append(val)

    def byte_SET_ADD(self, count):
        val = self.pop()
        the_set = self.peek(count)
        the_set.add(val)

    def byte_MAP_ADD(self, count):
        val, key = self.popn(2)
        the_map = self.peek(count)
        the_map[key] = val

    ## Printing

    if 0:   # Only used in the interactive interpreter, not in modules.
        def byte_PRINT_EXPR(self):
            print(self.pop())

    def byte_PRINT_ITEM(self):
        item = self.pop()
        self.print_item(item)

    def byte_PRINT_ITEM_TO(self):
        to = self.pop()
        item = self.pop()
        self.print_item(item, to)

    def byte_PRINT_NEWLINE(self):
        self.print_newline()

    def byte_PRINT_NEWLINE_TO(self):
        to = self.pop()
        self.print_newline(to)

    def print_item(self, item, to=None):
        if to is None:
            to = sys.stdout
        if to.softspace:
            print(" ", end="", file=to)
            to.softspace = 0
        print(item, end="", file=to)
        if isinstance(item, str):
            if (not item) or (not item[-1].isspace()) or (item[-1] == " "):
                to.softspace = 1
        else:
            to.softspace = 1

    def print_newline(self, to=None):
        if to is None:
            to = sys.stdout
        print("", file=to)
        to.softspace = 0

    ## Jumps

    def byte_JUMP_FORWARD(self, jump):
        self.jump(jump)

    def byte_JUMP_ABSOLUTE(self, jump):
        self.jump(jump)

    if 0:   # Not in py2.7
        def byte_JUMP_IF_TRUE(self, jump):
            val = self.top()
            if val:
                self.jump(jump)

        def byte_JUMP_IF_FALSE(self, jump):
            val = self.top()
            if not val:
                self.jump(jump)

    def byte_POP_JUMP_IF_TRUE(self, jump):
        val = self.pop()
        if val:
            self.jump(jump)

    def byte_POP_JUMP_IF_FALSE(self, jump):
        val = self.pop()
        if not val:
            self.jump(jump)

    def byte_JUMP_IF_TRUE_OR_POP(self, jump):
        val = self.top()
        if val:
            self.jump(jump)
        else:
            self.pop()

    def byte_JUMP_IF_FALSE_OR_POP(self, jump):
        val = self.top()
        if not val:
            self.jump(jump)
        else:
            self.pop()

    ## Blocks

    def byte_SETUP_LOOP(self, dest):
        self.push_block('loop', dest)

    def byte_GET_ITER(self):
        self.push(iter(self.pop()))

    def byte_GET_YIELD_FROM_ITER(self):
        tos = self.top()
        if isinstance(tos, types.GeneratorType) or isinstance(tos, types.CoroutineType):
            return
        tos = self.pop()
        self.push(iter(tos))

    def byte_FOR_ITER(self, jump):
        iterobj = self.top()
        try:
            v = next(iterobj)
            self.push(v)
        except StopIteration:
            self.pop()
            self.jump(jump)

    def byte_BREAK_LOOP(self):
        return 'break'

    def byte_CONTINUE_LOOP(self, dest):
        # This is a trick with the return value.
        # While unrolling blocks, continue and return both have to preserve
        # state as the finally blocks are executed.  For continue, it's
        # where to jump to, for return, it's the value to return.  It gets
        # pushed on the stack for both, so continue puts the jump destination
        # into return_value.
        self.return_value = dest
        return 'continue'

    def byte_SETUP_EXCEPT(self, dest):
        self.push_block('setup-except', dest)

    def byte_SETUP_FINALLY(self, dest):
        self.push_block('finally', dest)

    def byte_END_FINALLY(self):
        v = self.pop()
        if isinstance(v, str):
            why = v
            if why in ('return', 'continue'):
                self.return_value = self.pop()
            if why == 'silenced':       # PY3
                block = self.pop_block()
                assert block.type == 'except-handler'
                self.unwind_block(block)
                why = None
        elif v is None:
            why = None
        elif issubclass(v, BaseException):
            exctype = v
            val = self.pop()
            tb = self.pop()
            self.last_exception = (exctype, val, tb)
            why = 'reraise'
        else:       # pragma: no cover
            raise VirtualMachineError("Confused END_FINALLY")
        return why

    def byte_POP_BLOCK(self):
        self.pop_block()

    if PY2:
        def byte_RAISE_VARARGS(self, argc):
            # NOTE: the dis docs are completely wrong about the order of the
            # operands on the stack!
            exctype = val = tb = None
            if argc == 0:
                exctype, val, tb = self.last_exception
            elif argc == 1:
                exctype = self.pop()
            elif argc == 2:
                val = self.pop()
                exctype = self.pop()
            elif argc == 3:
                tb = self.pop()
                val = self.pop()
                exctype = self.pop()

            # There are a number of forms of "raise", normalize them somewhat.
            if isinstance(exctype, BaseException):
                val = exctype
                exctype = type(val)

            self.last_exception = (exctype, val, tb)

            if tb:
                return 'reraise'
            else:
                return 'exception'

    elif PY3:
        def byte_RAISE_VARARGS(self, argc):
            cause = exc = tb = None
            if argc == 3:
                tb = self.pop()
                cause = self.pop()
                exc = self.pop()
            elif argc == 2:
                cause = self.pop()
                exc = self.pop()
            elif argc == 1:
                exc = self.pop()
            tb = tb if tb else traceback(self.frame, self.frame.f_lasti)
            return self.do_raise(exc, cause, tb)

        def do_raise(self, exc, cause, tb):
            if exc is None:         # reraise
                exc_type, val, tb = self.last_exception
                if exc_type is None:
                    return 'exception'      # error
                else:
                    return 'reraise'

            elif type(exc) == type:
                # As in `raise ValueError`
                exc_type = exc
                val = exc()             # Make an instance.
            elif isinstance(exc, BaseException):
                # As in `raise ValueError('foo')`
                exc_type = type(exc)
                val = exc
            else:
                return 'exception'      # error

            # If you reach this point, you're guaranteed that
            # val is a valid exception instance and exc_type is its class.
            # Now do a similar thing for the cause, if present.
            if cause:
                if type(cause) == type:
                    cause = cause()
                elif not isinstance(cause, BaseException):
                    return 'exception'  # error

                val.__cause__ = cause

            tb = traceback(self.frame, self.frame.f_lasti)
            self.last_exception = exc_type, val, tb
            pseudosys._exc_info = self.last_exception
            return 'exception'

    def byte_POP_EXCEPT(self):
        block = self.pop_block()
        if block.type != 'except-handler':
            raise Exception("popped block is not an except handler")
        self.unwind_block(block)

    def byte_SETUP_WITH(self, dest):
        ctxmgr = self.pop()
        self.push(ctxmgr.__exit__)
        ctxmgr_obj = ctxmgr.__enter__()
        if PY2:
            self.push_block('with', dest)
        elif PY3:
            self.push_block('finally', dest)
        self.push(ctxmgr_obj)

    def byte_WITH_CLEANUP_START(self):
        u = self.top()
        v = None
        w = None
        if u is None:
            exit_method = self.pop(1)
        elif isinstance(u, str):
            if u in {'return', 'continue'}:
                exit_method = self.pop(2)
            else:
                exit_method = self.pop(1)
        elif issubclass(u, BaseException):
            w, v, u = self.popn(3)
            tp, exc, tb = self.popn(3)
            exit_method = self.pop()
            self.push(tp, exc, tb)
            self.push(None)
            self.push(w, v, u)
            block = self.pop_block()
            assert block.type == 'except-handler'
            self.push_block(block.type, block.handler, block.level-1)

        res = exit_method(u, v, w)
        self.push(u)
        self.push(res)

    def byte_WITH_CLEANUP_FINISH(self):
        res = self.pop()
        u = self.pop()
        if type(u) is type and issubclass(u, BaseException) and res:
                self.push("silenced")

    def byte_WITH_CLEANUP(self):
        # The code here does some weird stack manipulation: the exit function
        # is buried in the stack, and where depends on what's on top of it.
        # Pull out the exit function, and leave the rest in place.
        v = w = None
        u = self.top()
        if u is None:
            exit_func = self.pop(1)
        elif isinstance(u, str):
            if u in ('return', 'continue'):
                exit_func = self.pop(2)
            else:
                exit_func = self.pop(1)
            u = None
        elif issubclass(u, BaseException):
            if PY2:
                w, v, u = self.popn(3)
                exit_func = self.pop()
                self.push(w, v, u)
            elif PY3:
                w, v, u = self.popn(3)
                tp, exc, tb = self.popn(3)
                exit_func = self.pop()
                self.push(tp, exc, tb)
                self.push(None)
                self.push(w, v, u)
                block = self.pop_block()
                assert block.type == 'except-handler'
                self.push_block(block.type, block.handler, block.level-1)
        else:       # pragma: no cover
            raise VirtualMachineError("Confused WITH_CLEANUP")
        exit_ret = exit_func(u, v, w)
        err = (u is not None) and bool(exit_ret)
        if err:
            # An error occurred, and was suppressed
            if PY2:
                self.popn(3)
                self.push(None)
            elif PY3:
                self.push('silenced')

    ## Functions

    def byte_MAKE_FUNCTION(self, argc):
        if PY3:
            name = self.pop()
        else:
            # Pushes a new function object on the stack. TOS is the code
            # associated with the function. The function object is defined to
            # have argc default parameters, which are found below TOS.
            name = None
        code = self.pop()
        globs = self.frame.f_globals
        if PY3 and sys.version_info.minor >= 6:
            closure = self.pop() if (argc & 0x8) else None
            ann = self.pop() if (argc & 0x4) else None
            kwdefaults = self.pop() if (argc & 0x2) else None
            defaults = self.pop() if (argc & 0x1) else None
            fn = Function(name, code, globs, defaults, kwdefaults, closure, self)
        else:
            defaults = self.popn(argc)
            fn = Function(name, code, globs, defaults, None, None, self)
        self.push(fn)

    def byte_LOAD_CLOSURE(self, name):
        self.push(self.frame.cells[name])

    def byte_MAKE_CLOSURE(self, argc):
        if PY3:
            # TODO: the py3 docs don't mention this change.
            name = self.pop()
        else:
            name = None
        closure, code = self.popn(2)
        defaults = self.popn(argc)
        globs = self.frame.f_globals
        fn = Function(name, code, globs, defaults, None, closure, self)
        self.push(fn)

    def byte_CALL_FUNCTION_EX(self, arg):
        # Calls a function. The lowest bit of flags indicates whether the
        # var-keyword argument is placed at the top of the stack. Below
        # the var-keyword argument, the var-positional argument is on the
        # stack. Below the arguments, the function object to call is placed.
        # Pops all function arguments, and the function itself off the stack,
        # and pushes the return value.
        # Note that this opcode pops at most three items from the stack.
        #Var-positional and var-keyword arguments are packed by
        #BUILD_TUPLE_UNPACK_WITH_CALL and BUILD_MAP_UNPACK_WITH_CALL.
        # new in 3.6
        varkw = self.pop() if (arg & 0x1) else {}
        varpos = self.pop()
        return self.call_function(0, varpos, varkw)

    def byte_CALL_FUNCTION(self, arg):
        # Calls a function. argc indicates the number of positional arguments.
        # The positional arguments are on the stack, with the right-most
        # argument on top. Below the arguments, the function object to call is
        # on the stack. Pops all function arguments, and the function itself
        # off the stack, and pushes the return value.
        # 3.6: Only used for calls with positional args
        return self.call_function(arg, [], {})

    def byte_CALL_FUNCTION_VAR(self, arg):
        args = self.pop()
        return self.call_function(arg, args, {})

    def byte_CALL_FUNCTION_KW(self, argc):
        if not(six.PY3 and sys.version_info.minor >= 6):
            kwargs = self.pop()
            return self.call_function(arg, [], kwargs)
        # changed in 3.6: keyword arguments are packed in a tuple instead
        # of a dict. argc indicates total number of args.
        kwargnames = self.pop()
        lkwargs = len(kwargnames)
        kwargs = self.popn(lkwargs)
        arg = argc - lkwargs
        return self.call_function(arg, [], dict(zip(kwargnames, kwargs)))

    def byte_CALL_FUNCTION_VAR_KW(self, arg):
        args, kwargs = self.popn(2)
        return self.call_function(arg, args, kwargs)

    def call_function(self, arg, args, kwargs):
        lenKw, lenPos = divmod(arg, 256)
        namedargs = {}
        for i in range(lenKw):
            key, val = self.popn(2)
            namedargs[key] = val
        namedargs.update(kwargs)
        posargs = self.popn(lenPos)
        posargs.extend(args)

        func = self.pop()
        if hasattr(func, '__name__'):
            if func.__name__ == 'getattr' and type(posargs[0]) is Function and posargs[1] == '__qualname__':
                # https://bugs.python.org/issue19073
                retval = posargs[0].__qname__
                self.push(retval)
                return

        frame = self.frame
        if hasattr(func, 'im_func'):
            # Methods get self as an implicit first parameter.
            if func.im_self:
                posargs.insert(0, func.im_self)
            # The first parameter must be the correct type.
            if not isinstance(posargs[0], func.im_class):
                raise TypeError(
                    'unbound method %s() must be called with %s instance '
                    'as first argument (got %s instance instead)' % (
                        func.im_func.func_name,
                        func.im_class.__name__,
                        type(posargs[0]).__name__,
                    )
                )
            func = func.im_func

        if isinstance(func, types.FunctionType) and Interpret_Original:
            defaults = func.__defaults__ or ()
            kwdefaults = func.__kwdefaults__ or ()
            byterun_func = Function(
                    func.__name__, func.__code__, func.__globals__,
                    defaults, kwdefaults, func.__closure__, self)
        else:
            byterun_func = func

        retval = byterun_func(*posargs, **namedargs)
        self.push(retval)

    def import_module(self, name, fromList, level):
        f = self.frame
        g = f.f_globals
        l = f.f_locals
        try:
            res = self.import_python_module(name, g, l, fromList, level)
        except NoSource as e:
            log.info("Unable to load [%s] falling back to system load" % name)
            res = __import__(name, g, l, fromList, level)
            Loaded[name] = res
            if hasattr(res, '__file__'):
                log.info("%s:[%s] failed due to %s " % (res.__file__, name, e))
        self.push(res)

    def import_python_module(self, modulename, glo, loc, fromlist, level, search=None):
        """Import a python module.
        `modulename` is the name of the module, possibly a dot-separated name.
        `fromlist` is the list of things to imported from the module.
        """
        if modulename in Loaded: return Loaded[modulename]
        if '.' not in modulename:
            res = self.eval_python_module(modulename, glo, loc, fromlist, level, search)
        else:
            pkgn, name = modulename.rsplit('.', 1)
            pkg = self.import_python_module(pkgn, glo, loc, fromlist, level)
            res = self.eval_python_module(name, glo, loc, fromlist, level, [pkg.__path__])
            # res is an attribute of pkg
            setattr(pkg, res.__name__, res)
        Loaded[modulename] = res
        return res

    def eval_python_module(self, modulename, glo, loc, fromList, level, search=None):
        mymod = self.find_module(modulename, glo, loc, fromList, level, search, True)
        if os.path.isdir(mymod.__file__): return mymod
        code = self.load_source(mymod.__file__)
        # Execute the source file.
        frame = self.make_frame(code, f_globals=mymod.__dict__, f_locals=mymod.__dict__)
        val = self.run_frame(frame) # ignore the returned value
        return mymod

    def load_source(self, sfn):
        try:
            with open(sfn, "rU") as source_file:
                source = source_file.read()
                if not source or source[-1] != '\n': source += '\n'
                return compile(source, sfn, "exec")
        except IOError as e:
            raise NoSource("module does not live in a file: %r" % modulename)

    def find_module(self, name, glo, loc, fromlist, level, searchpath=None, isfile=True):
        """
        `level` specifies whether to use absolute and/or relative.
            The default is -1 which is both absolute and relative
            0 means only absolute and positive values indicate number
            parent directories to search relative to the directory of module
            calling `__import__`
        """
        assert level <= 0 # we dont implement relative yet
        path = None
        if level == 0:
            path = find_module_absolute(name, searchpath, isfile)
        elif level > 0:
            path = find_module_relative(name, searchpath, isfile)
        else:
            res = find_module_absolute(name, searchpath, isfile)
            path = find_module_relative(name, searchpath, isfile) if not res \
                    else res

        if not path:
            v = imp.find_module(name, searchpath)
            if v and v[1]:
                path = v[1]
            else:
                raise NoSource("<%s> was not found" % name)
        fn = path
        mymod = types.ModuleType(name)
        if isfile and os.path.isdir(path):
            fn = "%s/__init__.py" % path
        mymod.__path__ = path
        mymod.__file__ = fn
        mymod.__builtins__ = glo['__builtins__']
        # mark the module as being loaded
        return mymod

    def byte_RETURN_VALUE(self):
        self.return_value = self.pop()
        if self.frame.generator:
            self.frame.generator.finished = True
        return "return"

    def byte_YIELD_VALUE(self):
        self.return_value = self.pop()
        return "yield"

    def byte_YIELD_FROM(self):
        u = self.pop()
        x = self.top()

        try:
            if not isinstance(x, Generator) or u is None:
                # Call next on iterators.
                retval = next(x)
            else:
                retval = x.send(u)
            self.return_value = retval
        except StopIteration as e:
            self.pop()
            self.push(e.value)
        else:
            # YIELD_FROM decrements f_lasti, so that it will be called
            # repeatedly until a StopIteration is raised.
            self.jump(self.frame.f_lasti - 1)
            # Returning "yield" prevents the block stack cleanup code
            # from executing, suspending the frame in its current state.
            return "yield"

    ## Importing

    def byte_IMPORT_NAME(self, name):
        level, fromlist = self.popn(2)
        if name == 'sys':
            self.push(pseudosys)
            return
        if Intercept_Imports:
            self.import_module(name, fromlist, level)
        else:
            frame = self.frame
            self.push(__import__(name, frame.f_globals, frame.f_locals, fromlist, level))

    def byte_IMPORT_STAR(self):
        # TODO: this doesn't use __all__ properly.
        mod = self.pop()
        for attr in dir(mod):
            if attr[0] != '_':
                self.frame.f_locals[attr] = getattr(mod, attr)

    def byte_IMPORT_FROM(self, name):
        mod = self.top()
        self.push(getattr(mod, name))

    ## And the rest...

    def byte_EXEC_STMT(self):
        stmt, globs, locs = self.popn(3)
        six.exec_(stmt, globs, locs)

    if PY2:
        def byte_BUILD_CLASS(self):
            name, bases, methods = self.popn(3)
            self.push(type(name, bases, methods))


    elif PY3:
        def byte_LOAD_BUILD_CLASS(self):
            # New in py3
            self.push(build_class)

        def byte_STORE_LOCALS(self):
            self.frame.f_locals = self.pop()

    if 0:   # Not in py2.7
        def byte_SET_LINENO(self, lineno):
            self.frame.f_lineno = lineno

if PY3:
    def build_class(func, name, *bases, **kwds):
        "Like __build_class__ in bltinmodule.c, but running in the bytevm VM."
        if not isinstance(func, Function):
            raise TypeError("func must be a function")
        if not isinstance(name, str):
            raise TypeError("name is not a string")
        metaclass = kwds.pop('metaclass', None)
        # (We don't just write 'metaclass=None' in the signature above
        # because that's a syntax error in Py2.)
        if metaclass is None:
            metaclass = type(bases[0]) if bases else type
        if isinstance(metaclass, type):
            metaclass = calculate_metaclass(metaclass, bases)

        try:
            prepare = metaclass.__prepare__
        except AttributeError:
            namespace = {}
        else:
            namespace = prepare(name, bases, **kwds)

        # Execute the body of func. This is the step that would go wrong if
        # we tried to use the built-in __build_class__, because __build_class__
        # does not call func, it magically executes its body directly, as we
        # do here (except we invoke our VirtualMachine instead of CPython's).
        frame = func._vm.make_frame(func.func_code,
                                    f_globals=func.func_globals,
                                    f_locals=namespace,
                                    f_closure=func.func_closure)
        cell = func._vm.run_frame(frame)

        cls = metaclass(name, bases, namespace)
        if isinstance(cell, Cell):
            cell.set(cls)
        return cls

    def calculate_metaclass(metaclass, bases):
        "Determine the most derived metatype."
        winner = metaclass
        for base in bases:
            t = type(base)
            if issubclass(t, winner):
                winner = t
            elif not issubclass(winner, t):
                raise TypeError("metaclass conflict", winner, t)
        return winner



def find_module_absolute(name, searchpath, isfile):
    # search path should really be appeneded to a list of paths
    # that the interpreter knows about. For now, we only look in '.'
    myname = name if not searchpath else "%s/%s" % (searchpath, name)
    if isfile:
        fname = "%s.py" % myname
        return os.path.abspath(fname) if os.path.isfile(fname) else None
    else:
        return os.path.abspath(myname) if os.path.isdir(myname) else None

def find_module_relative(name, searchpath): return None

