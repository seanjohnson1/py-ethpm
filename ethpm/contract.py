from typing import Any, Dict, List, Tuple, Type  # noqa: F401

from eth_utils import combomethod, is_canonical_address, to_bytes, to_checksum_address
from eth_utils.toolz import assoc, curry, pipe
from web3 import Web3
from web3.contract import Contract

from ethpm.exceptions import BytecodeLinkingError, ValidationError
from ethpm.validation import validate_address, validate_empty_bytes


class LinkableContract(Contract):
    """
    A subclass of web3.contract.Contract that is capable of handling
    contract factories with link references in their package's manifest.
    """

    unlinked_references: Tuple[Dict[str, Any]] = None
    linked_references: Tuple[Dict[str, Any]] = None
    needs_bytecode_linking = None

    def __init__(self, address: bytes = None, **kwargs: Any) -> None:
        if self.needs_bytecode_linking:
            raise BytecodeLinkingError(
                "Contract cannot be instantiated until its bytecode is linked."
            )
        validate_address(address)
        # todo: remove automatic checksumming of address once web3 dep is updated in pytest-ethereum
        super(LinkableContract, self).__init__(
            address=to_checksum_address(address), **kwargs
        )

    @classmethod
    def factory(
        cls, web3: Web3, class_name: str = None, **kwargs: Any
    ) -> "LinkableContract":
        dep_link_refs = kwargs.get("unlinked_references")
        bytecode = kwargs.get("bytecode")
        needs_bytecode_linking = False
        if dep_link_refs and bytecode:
            if not is_prelinked_bytecode(to_bytes(hexstr=bytecode), dep_link_refs):
                needs_bytecode_linking = True
        kwargs = assoc(kwargs, "needs_bytecode_linking", needs_bytecode_linking)
        return super(LinkableContract, cls).factory(web3, class_name, **kwargs)

    @classmethod
    def constructor(cls, *args: Any, **kwargs: Any) -> bool:
        if cls.needs_bytecode_linking:
            raise BytecodeLinkingError(
                "Contract cannot be deployed until its bytecode is linked."
            )
        return super(LinkableContract, cls).constructor(*args, **kwargs)

    @classmethod
    def link_bytecode(cls, attr_dict: Dict[str, str]) -> Type["LinkableContract"]:
        """
        Return a cloned contract factory with the deployment / runtime bytecode linked.

        :attr_dict: Dict[`ContractType`: `Address`] for all deployment and runtime link references.
        """
        if not cls.unlinked_references and not cls.linked_references:
            raise BytecodeLinkingError("Contract factory has no linkable bytecode.")
        if not cls.needs_bytecode_linking:
            raise BytecodeLinkingError(
                "Bytecode for this contract factory does not require bytecode linking."
            )
        cls.validate_attr_dict(attr_dict)
        bytecode = apply_all_link_refs(cls.bytecode, cls.unlinked_references, attr_dict)
        runtime = apply_all_link_refs(
            cls.bytecode_runtime, cls.linked_references, attr_dict
        )
        linked_class = cls.factory(
            cls.web3, bytecode_runtime=runtime, bytecode=bytecode
        )
        if linked_class.needs_bytecode_linking:
            raise BytecodeLinkingError(
                "Expected class to be fully linked, but class still needs bytecode linking."
            )
        return linked_class

    @combomethod
    def validate_attr_dict(self, attr_dict: Dict[str, str]) -> None:
        """
        Validates that ContractType keys in attr_dict reference existing manifest ContractTypes.
        """
        attr_dict_names = list(attr_dict.keys())

        if not self.unlinked_references and not self.linked_references:
            raise BytecodeLinkingError(
                "Unable to validate attr dict, this contract has no linked/unlinked references."
            )

        all_link_refs: Tuple[Any, ...]
        if self.unlinked_references and self.linked_references:
            all_link_refs = self.unlinked_references + self.linked_references
        elif not self.unlinked_references:
            all_link_refs = self.linked_references
        else:
            all_link_refs = self.unlinked_references

        all_link_names = [ref["name"] for ref in all_link_refs]
        if set(attr_dict_names) != set(all_link_names):
            raise BytecodeLinkingError(
                "All link references must be defined when calling "
                "`link_bytecode` on a contract factory."
            )
        for address in attr_dict.values():
            if not is_canonical_address(address):
                raise BytecodeLinkingError(
                    f"Address: {address} as specified in the attr_dict is not "
                    "a valid canoncial address."
                )


def is_prelinked_bytecode(bytecode: bytes, link_refs: List[Dict[str, Any]]) -> bool:
    """
    Returns False if all expected link_refs are unlinked, otherwise returns True.
    todo support partially pre-linked bytecode (currently all or nothing)
    """
    for link_ref in link_refs:
        for offset in link_ref["offsets"]:
            try:
                validate_empty_bytes(offset, link_ref["length"], bytecode)
            except ValidationError:
                return True
    return False


def apply_all_link_refs(
    bytecode: bytes, link_refs: List[Dict[str, Any]], attr_dict: Dict[str, str]
) -> bytes:
    """
    Applies all link references corresponding to a valid attr_dict to the bytecode.
    """
    if link_refs is None:
        return bytecode
    link_fns = (
        apply_link_ref(offset, ref["length"], attr_dict[ref["name"]])
        for ref in link_refs
        for offset in ref["offsets"]
    )
    linked_bytecode = pipe(bytecode, *link_fns)
    return linked_bytecode


@curry
def apply_link_ref(offset: int, length: int, value: bytes, bytecode: bytes) -> bytes:
    """
    Returns the new bytecode with `value` put into the location indicated by `offset` and `length`.
    """
    try:
        validate_empty_bytes(offset, length, bytecode)
    except ValidationError:
        raise BytecodeLinkingError("Link references cannot be applied to bytecode")

    new_bytes = (
        # Ignore linting error b/c conflict b/w black & flake8
        bytecode[:offset]
        + value
        + bytecode[offset + length :]  # noqa: E201, E203
    )
    return new_bytes
