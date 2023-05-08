"""
code walker
"""

from copy import copy
from types import ModuleType

from xasm.assemble import (
    Assembler,
    create_code
)
from xdis.cross_dis import op_size

from .utils import Instruction
from .patch import InPlacePatcher
from .rules import RULE_APPLIER


def walk_codes(opc: ModuleType, asm: Assembler, is_pypy: bool, rule_applier: RULE_APPLIER) -> Assembler:
    """
    Walk through the codes and downgrade them

    :param opc: opcode map (it's a module ig)
    :param asm: input Assembler
    :param is_pypy: set if is PyPy
    :param rule_applier: rule applier
    :return: output Assembler, None if failed
    """

    new_asm = Assembler(asm.python_version, is_pypy)
    new_asm.size = asm.size

    for code_idx, old_code in enumerate(asm.codes):
        new_code = copy(old_code)
        new_label = copy(asm.label[code_idx])
        old_backpatch_inst = asm.backpatch[code_idx]
        new_backpatch_inst: set[Instruction] = set()
        new_code.co_lnotab = copy(old_code.co_lnotab)
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

        patcher = InPlacePatcher(opc, new_code, new_label, new_backpatch_inst)
        rule_applier(patcher, is_pypy)

        # messes are done, fix the stuffs xDD
        patcher.fix_all()

        new_asm.code = new_code
        # this assembles the instructions and writes the code.co_code
        # after that it also freezes the code object
        co = create_code(new_asm, new_label, new_backpatch_inst)
        # append data to lists, also backup the code
        # TODO: i hope i understand this correctly
        new_asm.update_lists(co, new_label, new_backpatch_inst)

    # TODO: why is this getting reversed?
    new_asm.code_list.reverse()
    # TODO: what does this do?
    new_asm.finished = 'finished'
    return new_asm