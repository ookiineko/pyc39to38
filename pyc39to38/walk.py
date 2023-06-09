"""
code walker
"""

from copy import copy
from types import ModuleType
from traceback import print_exc
from typing import (
    Optional,
    Set,
    Dict,
    Callable
)
from logging import getLogger

from xasm.assemble import (
    Assembler,
    create_code
)
from xdis.cross_dis import (
    op_size,
    findlinestarts
)
from xdis.codetype.code38 import Code38
from xdis.codetype.base import iscode
from xdis.version_info import PYTHON_VERSION_TRIPLE

from .utils import (
    Instruction,
    build_inst,
    genlinestarts
)
from .patch import InPlacePatcher
from .rules import RULE_APPLIER
from .cfg import Config
from . import PY38_VER


logger = getLogger('walk')

EXTENDED_ARG = 'EXTENDED_ARG'


def walk_codes(opc: ModuleType, asm: Assembler, is_pypy: bool,
               cfg: Config, rule_applier: RULE_APPLIER) -> Optional[Assembler]:
    """
    Walk through the codes and downgrade them

    :param opc: opcode map (it's a module ig)
    :param asm: input Assembler
    :param is_pypy: set if is PyPy
    :param cfg: config options
    :param rule_applier: rule applier
    :return: output Assembler, None if failed
    """

    new_asm = Assembler(PY38_VER, is_pypy)
    new_asm.size = asm.size

    methods: Dict[str, Code38] = {}

    for code_idx, old_code in enumerate(asm.codes):
        new_code = copy(old_code)
        new_label = copy(asm.label[code_idx])
        old_backpatch_inst = asm.backpatch[code_idx]
        new_backpatch_inst: Set[Instruction] = set()
        new_code.co_lnotab = dict(findlinestarts(old_code))
        new_insts = []
        for old_inst in old_code.instructions:
            new_inst = copy(old_inst)
            new_insts.append(new_inst)
            if old_inst in old_backpatch_inst:
                # restore the backpatch tag
                if new_inst.opcode in opc.JREL_OPS:
                    new_inst.arg += new_inst.offset + op_size(new_inst.opcode, opc)
                new_inst.arg = f'L{new_inst.arg}'

                new_backpatch_inst.add(new_inst)
        new_code.instructions = new_insts
        # TODO: IDK when the `instructions` is going to be removed

        # note that patch can change the label and backpatch_inst
        patcher = InPlacePatcher(opc, new_code, new_label, new_backpatch_inst)

        # before applying the patches, we need to remove EXTENDED_ARG
        shift_on_add_extarg: Set[Instruction] = set()
        for inst_idx in range(len(patcher.code.instructions) - 1, -1, -1):
            inst = patcher.code.instructions[inst_idx]
            if inst.opname == EXTENDED_ARG:
                _, _, label, line_no = patcher.pop_inst(inst_idx)
                next_inst = patcher.code.instructions[inst_idx]
                # if the removed inst has a label, we need some extra handling
                if label:
                    # if next inst has label, we need to redirect all reference of the original label to it
                    for iterating_label, label_off in patcher.label.items():
                        if label_off == next_inst.offset:
                            # replace all reference of the original label to the label of next inst
                            for inst in patcher.code.instructions:
                                if patcher.need_backpatch(inst):
                                    # this inst has a label as arg
                                    if inst.arg == label:
                                        inst.arg = iterating_label
                            break
                    else:
                        # no label found for next inst, just add the original label back to there
                        patcher.label[label] = next_inst.offset
                # restore the line number if needed
                if line_no:
                    patcher.code.co_lnotab[next_inst.offset] = line_no
                else:
                    # see if the next inst has a line number
                    if next_inst.offset in patcher.code.co_lnotab:
                        # we may want to shift the line number if we are going to re-add EXTENDED_ARG
                        if cfg.preserve_lineno_after_extarg:
                            shift_on_add_extarg.add(next_inst)

        try:
            rule_applier(patcher, is_pypy, cfg)
        except (ValueError, TypeError):
            logger.error(f'failed to apply rules for code #{code_idx}:')
            print_exc()
            return None

        # add back the EXTENDED_ARG where needed
        while True:
            dirty_insert = False
            for inst_idx, inst in enumerate(patcher.code.instructions):
                if patcher.need_backpatch(inst):
                    # this inst has a label as arg
                    # deref the label
                    label_off = patcher.label[inst.arg]
                    # calculate the real arg
                    if inst.opcode in opc.JREL_OPS:
                        arg = label_off - inst.offset - op_size(inst.opcode, opc)
                    elif inst.opcode in opc.JABS_OPS:
                        arg = label_off
                    else:
                        raise ValueError(f'unsupported jump opcode {inst.opname} at idx {inst_idx} in code #{code_idx}')
                    # if the arg is bigger than one byte, we need to add EXTENDED_ARG
                    # the arg for EXTENDED_ARG is how many extra bytes we need to extend
                    if arg > 255:
                        # check if we already have an EXTENDED_ARG on top
                        if inst_idx > 0 and (
                                last_inst := patcher.code.instructions[inst_idx - 1]
                        ).opname == EXTENDED_ARG:
                            # this is after the first run, we need to update the arg
                            last_inst.arg = arg // 256
                        else:
                            # we need to add EXTENDED_ARG
                            size = op_size(opc.opmap[EXTENDED_ARG], opc)
                            extended_arg_inst = build_inst(patcher.opc, EXTENDED_ARG, arg // 256)
                            # get the next inst
                            next_inst = patcher.code.instructions[inst_idx]
                            if cfg.preserve_lineno_after_extarg:
                                shift_on_add = next_inst in shift_on_add_extarg
                            else:
                                shift_on_add = False
                            patcher.insert_inst(extended_arg_inst, size, inst_idx, None, shift_on_add)
                            dirty_insert = True
                            # if the next inst has a label, just set it to here
                            # iterate all labels
                            for iterating_label, label_off in patcher.label.items():
                                if label_off == next_inst.offset:
                                    # set the offset to this inst
                                    patcher.label[iterating_label] = extended_arg_inst.offset
                                    break
                            break
            if not dirty_insert:
                break

        try:
            # messes are done, fix the stuffs xDD
            patcher.fix_all()
        except ValueError:
            logger.error(f'failed to fix the code #{code_idx}:')
            print_exc()
            return None

        new_asm.code = new_code
        # fix the code objects in constants
        const_is_tuple = isinstance(new_asm.code.co_consts, tuple)
        if const_is_tuple:
            new_asm.code.co_consts = list(new_asm.code.co_consts)
        for idx, const in enumerate(new_asm.code.co_consts):
            if iscode(const):
                if const.co_name in methods:
                    new_asm.code.co_consts[idx] = methods[const.co_name]
                else:
                    logger.error(f'missing method \'{const.co_name}\' in code #{code_idx}')
                    return None
        if const_is_tuple:
            new_asm.code.co_consts = tuple(new_asm.code.co_consts)

        # backup the lnotab for the following hack
        lnotab_backup = new_code.co_lnotab

        native_code = new_asm.python_version[:2] == PYTHON_VERSION_TRIPLE[:2]
        old_to_native: Optional[Callable] = None

        if native_code:
            old_to_native = new_code.to_native
            new_code.to_native = new_code.freeze

        # this assembles the instructions and writes the code.co_code
        # after that it also freezes the code object
        co = create_code(new_asm, patcher.label, patcher.backpatch_inst)

        # rollback the lnotab for the hack
        co.co_lnotab = lnotab_backup
        try:
            # HACK: xasm has a bug encoding lnotab with big numbers and negative line number increments
            #       use our own lnotab encoder here to fix it
            co.co_lnotab = genlinestarts(co)
        except ValueError:
            logger.error(f'failed to fix the line number table for code #{code_idx}:')
            print_exc()
            return None

        if native_code:
            old_to_native()

        # register the method name
        methods[co.co_name] = co
        # append data to lists, also backup the code
        # TODO: i hope i understand this correctly
        new_asm.update_lists(co, patcher.label, patcher.backpatch_inst)

    # TODO: why is this getting reversed?
    new_asm.code_list.reverse()
    # TODO: what does this do?
    new_asm.finished = 'finished'
    return new_asm
