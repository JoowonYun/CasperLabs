#!/usr/bin/env python3
"""
CasperLabs Client API library and command line tool.
"""

# Hack to fix the relative imports problems #
import sys
from pathlib import Path

file = Path(__file__).resolve()
parent, root = file.parent, file.parents[1]
sys.path.append(str(root))

# end of hack #
import time
import argparse
import grpc
from grpc._channel import _Rendezvous
import ssl
import functools
from pyblake2 import blake2b
import ed25519
import base64
import struct
import json
from operator import add
from functools import reduce
from itertools import dropwhile
import logging

# Monkey patching of google.protobuf.text_encoding.CEscape
# to get keys and signatures in hex when printed
import google.protobuf.text_format

CEscape = google.protobuf.text_format.text_encoding.CEscape


def _hex(text, as_utf8):
    try:
        return (len(text) in (32, 64)) and text.hex() or CEscape(text, as_utf8)
    except TypeError:
        return CEscape(text, as_utf8)


google.protobuf.text_format.text_encoding.CEscape = _hex

# ~/CasperLabs/protobuf/io/casperlabs/node/api/control.proto
from .control_pb2_grpc import ControlServiceStub
from . import control_pb2 as control

# ~/CasperLabs/protobuf/io/casperlabs/node/api/casper.proto
from . import casper_pb2 as casper
from .casper_pb2_grpc import CasperServiceStub

# ~/CasperLabs/protobuf/io/casperlabs/casper/consensus/consensus.proto
from . import consensus_pb2 as consensus

# ~/CasperLabs/protobuf/io/casperlabs/casper/consensus/info.proto
from . import info_pb2 as info

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 40401
DEFAULT_INTERNAL_PORT = 40402


class ABI:
    """
    Encode (serialize) deploy args.

    Currently supported ABI types:
    - unsigned integers: u32, u64, u512
    - byte_array, an array of bytes of arbitrary length,
    - account, 32 bytes long byte_array, used for encoding of public keys.

    There are two ways to serialize a list of deploy arguments:

    1. Using method ABI.args, for example:

        ABI.args([ABI.u32(100), ABI.u64(5000)])

      Arguments are encoded with methods appropriate for their types,
      and passed in a list to method ABI.args.

      This is the recommended way of serializing ABI arguments in Python code.

    2. By encoding arguments in a JSON format and passing them to method
      ABI.args_from_json, for example:

        ABI.args_from_json(json_string)

      See documentation of ABI.args_from_json for details of the JSON format.

      This method has been developed to support passing deploy arguments
      on command line.
    """

    INTEGER_TYPES = ("u32", "u64", "u512", "int_value", "long_value", "big_int")
    BYTE_ARRAY_TYPES = ("byte_array", "account", "bytes_value")
    OPTIONAL_TYPES = ("option", "optional_value")

    ALL_TYPES = INTEGER_TYPES + BYTE_ARRAY_TYPES + OPTIONAL_TYPES

    @staticmethod
    def option(o: bytes) -> bytes:
        if o is None:
            return bytes([0])
        return bytes([1]) + o

    @staticmethod
    def optional_value(o: bytes) -> bytes:
        return ABI.option(o)

    @staticmethod
    def u32(n: int) -> bytes:
        return struct.pack("<I", n)

    @staticmethod
    def u64(n: int) -> bytes:
        return struct.pack("<Q", n)

    @staticmethod
    def u512(n: int) -> bytes:
        bs = list(
            dropwhile(
                lambda b: b == 0,
                reversed(n.to_bytes(64, byteorder="little", signed=False)),
            )
        )
        return len(bs).to_bytes(1, byteorder="little", signed=False) + bytes(
            reversed(bs)
        )

    @staticmethod
    def byte_array(a: bytes) -> bytes:
        return ABI.u32(len(a)) + a

    @staticmethod
    def bytes_value(a: bytes) -> bytes:
        return ABI.byte_array(a)

    @staticmethod
    def account(a: bytes) -> bytes:
        if len(a) != 32:
            raise Exception("Account must be 32 bytes long")
        return ABI.byte_array(a)

    @staticmethod
    def int_value(a: int) -> bytes:
        # TODO: should be signed 32 bits
        return ABI.u32(a)

    def long_value(a: int) -> bytes:
        # TODO: should be signed 64 bits
        return ABI.u64(a)

    def big_int(a) -> bytes:
        try:
            return ABI.u512(int(a["value"]))
        except TypeError:
            return ABI.u512(int(a))

    @staticmethod
    def args(l: list) -> bytes:
        return ABI.u32(len(l)) + reduce(add, map(ABI.byte_array, l))

    @staticmethod
    def args_from_json(s: str) -> bytes:
        """
        Convert a string with JSON representation of deploy args to binary (ABI).

        The JSON should be a list of dictionaries each representing one arg,
        for example:

            [
                {"name": "amount", "value": {"long_value": 123456}},
                {"name": "account", "value": {"bytes_value": '0000000000000000000000000000000000000000000000000000000000000000'},
                {"name": "purse_id", "value": {"optional_value": {}}},
            ]

        """
        args = json.loads(s)

        def python_value(typ, value):
            if typ in ("big_int",):
                try:
                    # new style proto3 JSON
                    return int(value["value"])
                except TypeError:
                    # compatibility mode
                    return int(value)
            if typ in ABI.INTEGER_TYPES:
                return int(value)
            elif typ in ABI.BYTE_ARRAY_TYPES:
                return bytearray.fromhex(value)
            elif typ in ABI.OPTIONAL_TYPES:
                if not value:
                    return None
                return encode({"value": value})
            raise ValueError(f"Unknown type {typ}, expected one of {ABI.ALL_TYPES}")

        def encode(arg) -> bytes:
            typ, value = list(arg["value"].items())[0]
            v = python_value(typ, value)
            return getattr(ABI, typ)(v)

        return ABI.args([encode(arg) for arg in args])


def read_pem_key(file_name: str):
    with open(file_name) as f:
        s = [l for l in f.readlines() if l and not l.startswith("-----")][0].strip()
        r = base64.b64decode(s)
        return len(r) % 32 == 0 and r[:32] or r[-32:]


class InternalError(Exception):
    """
    The only exception that API calls can throw.
    Internal errors like gRPC exceptions will be caught
    and this exception thrown instead, so the user does
    not have to worry about handling any other exceptions.
    """

    def __init__(self, status="", details=""):
        super(InternalError, self).__init__()
        self.status = status
        self.details = details

    def __str__(self):
        return f"{self.status}: {self.details}"


def api(function):
    """
    Decorator of API functions that protects user code from
    unknown exceptions raised by gRPC or internal API errors.
    It will catch all exceptions and throw InternalError.

    :param function: function to be decorated
    :return:
    """

    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (SyntaxError, TypeError, InternalError):
            raise
        except _Rendezvous as e:
            raise InternalError(str(e.code()), e.details())
        except Exception as e:
            raise InternalError(details=str(e)) from e

    return wrapper


def _hash(data: bytes) -> bytes:
    h = blake2b(digest_size=32)
    h.update(data)
    return h.digest()


def _read_binary(file_name: str):
    with open(file_name, "rb") as f:
        return f.read()


def _encode_contract(contract_options, contract_args):
    """
    """
    file_name, hash, name, uref = contract_options
    C = consensus.Deploy.Code
    if file_name:
        return C(wasm=_read_binary(file_name), abi_args=contract_args)
    if hash:
        return C(hash=hash, abi_args=contract_args)
    if name:
        return C(name=name, abi_args=contract_args)
    if uref:
        return C(uref=uref, abi_args=contract_args)
    raise Exception("One of wasm, hash, name or uref is required")


def _sign(private_key, data: bytes):
    return private_key and consensus.Signature(
        sig_algorithm="ed25519",
        sig=ed25519.SigningKey(read_pem_key(private_key)).sign(data),
    )


def _serialize(o) -> bytes:
    return o.SerializeToString()


class InsecureGRPCService:
    def __init__(self, host, port, serviceStub):
        self.address = f"{host}:{port}"
        self.serviceStub = serviceStub

    def __getattr__(self, name):
        def f(*args):
            with grpc.insecure_channel(self.address) as channel:
                return getattr(self.serviceStub(channel), name)(*args)

        def g(*args):
            with grpc.insecure_channel(self.address) as channel:
                yield from getattr(self.serviceStub(channel), name[: -len("_stream")])(
                    *args
                )

        return name.endswith("_stream") and g or f


def extract_common_name(certificate_file: str) -> str:
    cert_dict = ssl._ssl._test_decode_cert(certificate_file)
    return [t[0][1] for t in cert_dict["subject"] if t[0][0] == "commonName"][0]


class SecureGRPCService:
    def __init__(self, host, port, serviceStub, node_id, certificate_file):
        self.address = f"{host}:{port}"
        self.serviceStub = serviceStub
        self.node_id = node_id  # or extract_common_name(certificate_file)
        self.certificate_file = certificate_file
        with open(self.certificate_file, "rb") as f:
            self.credentials = grpc.ssl_channel_credentials(f.read())
        self.secure_channel_options = self.node_id and (
            ("grpc.ssl_target_name_override", self.node_id),
            ("grpc.default_authority", self.node_id),
        )

    def __getattr__(self, name):
        def f(*args):
            with grpc.secure_channel(
                self.address, self.credentials, options=self.secure_channel_options
            ) as channel:
                return getattr(self.serviceStub(channel), name)(*args)

        def g(*args):
            with grpc.secure_channel(
                self.address, self.credentials, options=self.secure_channel_options
            ) as channel:
                yield from getattr(self.serviceStub(channel), name[: -len("_stream")])(
                    *args
                )

        return name.endswith("_stream") and g or f


class CasperLabsClient:
    """
    gRPC CasperLabs client.
    """

    # Note, there is also casper.StateQuery.KeyVariant.KEY_VARIANT_UNSPECIFIED,
    # but it doesn't seem to have an official string representation
    # ("key_variant_unspecified"? "unspecified"?) and is not used by the client.
    STATE_QUERY_KEY_VARIANT = {
        "hash": casper.StateQuery.KeyVariant.HASH,
        "uref": casper.StateQuery.KeyVariant.UREF,
        "address": casper.StateQuery.KeyVariant.ADDRESS,
        "local": casper.StateQuery.KeyVariant.LOCAL,
    }

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        internal_port: int = DEFAULT_INTERNAL_PORT,
        node_id: str = None,
        certificate_file: str = None,
    ):
        """
        CasperLabs client's constructor.

        :param host:            Hostname or IP of node on which gRPC service is running
        :param port:            Port used for external gRPC API
        :param internal_port:   Port used for internal gRPC API
        :param certificate_file:      Certificate file for TLS
        :param node_id:         node_id of the node, for gRPC encryption
        """
        self.host = host
        self.port = port
        self.internal_port = internal_port
        self.node_id = node_id
        self.certificate_file = certificate_file

        if node_id:
            self.casperService = SecureGRPCService(
                host, port, CasperServiceStub, node_id, certificate_file
            )
            self.controlService = SecureGRPCService(
                # We currently assume that if node_id is given then
                # we get certificate_file too. This is unlike in the Scala client
                # where node_id is all that's needed for configuring secure connection.
                # The reason for this is that currently it doesn't seem to be possible
                # to open a secure grpc connection in Python without supplying any
                # certificate on the client side.
                host,
                internal_port,
                ControlServiceStub,
                node_id,
                certificate_file,
            )
        else:
            self.casperService = InsecureGRPCService(host, port, CasperServiceStub)
            self.controlService = InsecureGRPCService(
                host, internal_port, ControlServiceStub
            )

    @api
    def deploy(
        self,
        from_addr: bytes = None,
        gas_price: int = 10,
        payment: str = None,
        session: str = None,
        public_key: str = None,
        private_key: str = None,
        session_args: bytes = None,
        payment_args: bytes = None,
        payment_hash: bytes = None,
        payment_name: str = None,
        payment_uref: bytes = None,
        session_hash: bytes = None,
        session_name: str = None,
        session_uref: bytes = None,
    ):
        """
        Deploy a smart contract source file to Casper on an existing running node.
        The deploy will be packaged and sent as a block to the network depending
        on the configuration of the Casper instance.

        :param from_addr:     Purse address that will be used to pay for the deployment.
        :param gas_price:     The price of gas for this transaction in units dust/gas.
                              Must be positive integer.
        :param payment:       Path to the file with payment code.
        :param session:       Path to the file with session code.
        :param public_key:    Path to a file with public key (Ed25519)
        :param private_key:   Path to a file with private key (Ed25519)
        :param session_args:  List of ABI encoded arguments of session contract
        :param payment_args:  List of ABI encoded arguments of payment contract
        :param session-hash:  Hash of the stored contract to be called in the
                              session; base16 encoded.
        :param session-name:  Name of the stored contract (associated with the
                              executing account) to be called in the session.
        :param session-uref:  URef of the stored contract to be called in the
                              session; base16 encoded.
        :param payment-hash:  Hash of the stored contract to be called in the
                              payment; base16 encoded.
        :param payment-name:  Name of the stored contract (associated with the
                              executing account) to be called in the payment.
        :param payment-uref:  URef of the stored contract to be called in the
                              payment; base16 encoded.
        :return:              Tuple: (deserialized DeployServiceResponse object, deploy_hash)
        """
        if from_addr and len(from_addr) != 32:
            raise Exception(f"from_addr must be 32 bytes")

        session_options = (session, session_hash, session_name, session_uref)
        payment_options = (payment, payment_hash, payment_name, payment_uref)

        # Compatibility mode, should be removed when payment is obligatory
        if len(list(filter(None, payment_options))) == 0:
            logging.info("No payment contract provided, using session as payment")
            payment_options = session_options

        if len(list(filter(None, session_options))) != 1:
            raise TypeError(
                "deploy: only one of session, session_hash, session_name, session_uref must be provided"
            )

        if len(list(filter(None, payment_options))) != 1:
            raise TypeError(
                "deploy: only one of payment, payment_hash, payment_name, payment_uref must be provided"
            )

        # session_args must go to payment as well for now cause otherwise we'll get GASLIMIT error,
        # if payment is same as session:
        # https://github.com/CasperLabs/CasperLabs/blob/dev/casper/src/main/scala/io/casperlabs/casper/util/ProtoUtil.scala#L463
        body = consensus.Deploy.Body(
            session=_encode_contract(session_options, session_args),
            payment=_encode_contract(
                payment_options,
                payment_options == session_options and session_args or payment_args,
            ),
        )

        approval_public_key = public_key and read_pem_key(public_key)
        account_public_key = from_addr or approval_public_key

        header = consensus.Deploy.Header(
            account_public_key=account_public_key,
            timestamp=int(time.time()),
            gas_price=gas_price,
            body_hash=_hash(_serialize(body)),
        )

        deploy_hash = _hash(_serialize(header))
        approvals = (
            []
            if not account_public_key
            else [
                consensus.Approval(
                    approver_public_key=approval_public_key,
                    signature=_sign(private_key, deploy_hash),
                )
            ]
        )
        d = consensus.Deploy(
            deploy_hash=deploy_hash, approvals=approvals, header=header, body=body
        )

        # TODO: Deploy returns Empty, error handing via exceptions, apparently,
        # so no point in returning it.
        return self.casperService.Deploy(casper.DeployRequest(deploy=d)), deploy_hash

    @api
    def showBlocks(self, depth: int = 1, max_rank=0, full_view=True):
        """
        Get slices of the DAG, going backwards, rank by rank.

        :param depth:     How many of the top ranks of the DAG to show.
        :param max_rank:  Maximum rank to go back from.
                          0 means go from the current tip of the DAG.
        :param full_view: Full view if True, otherwise basic.
        :return:          Generator of block info objects.
        """
        yield from self.casperService.StreamBlockInfos_stream(
            casper.StreamBlockInfosRequest(
                depth=depth,
                max_rank=max_rank,
                view=(
                    full_view and info.BlockInfo.View.FULL or info.BlockInfo.View.BASIC
                ),
            )
        )

    @api
    def showBlock(self, block_hash_base16: str, full_view=True):
        """
        Returns object describing a block known by Casper on an existing running node.

        :param block_hash_base16: hash of the block to be retrieved
        :param full_view:         full view if True, otherwise basic
        :return:                  object representing the retrieved block
        """
        return self.casperService.GetBlockInfo(
            casper.GetBlockInfoRequest(
                block_hash_base16=block_hash_base16,
                view=(
                    full_view and info.BlockInfo.View.FULL or info.BlockInfo.View.BASIC
                ),
            )
        )

    @api
    def propose(self):
        """"
        Propose a block using deploys in the pool.

        :return:    response object with block_hash
        """
        return self.controlService.Propose(control.ProposeRequest())

    @api
    def visualizeDag(
        self,
        depth: int,
        out: str = None,
        show_justification_lines: bool = False,
        stream: str = None,
    ):
        """
        Retrieve DAG in DOT format.

        :param depth:                     depth in terms of block height
        :param out:                       output image filename, outputs to stdout if
                                          not specified, must end with one of the png,
                                          svg, svg_standalone, xdot, plain, plain_ext,
                                          ps, ps2, json, json0
        :param show_justification_lines:  if justification lines should be shown
        :param stream:                    subscribe to changes, 'out' has to specified,
                                          valid values are 'single-output', 'multiple-outputs'
        :return:                          VisualizeBlocksResponse object
        """
        raise Exception("Not implemented yet")

    @api
    def queryState(self, blockHash: str, key: str, path: str, keyType: str):
        """
        Query a value in the global state.

        :param blockHash:         Hash of the block to query the state of
        :param key:               Base16 encoding of the base key
        :param path:              Path to the value to query. Must be of the form
                                  'key1/key2/.../keyn'
        :param keyType:           Type of base key. Must be one of 'hash', 'uref', 'address' or 'local'.
                                  For 'local' key type, 'key' value format is {seed}:{rest},
                                  where both parts are hex encoded."
        :return:                  QueryStateResponse object
        """

        def key_variant(keyType):

            variant = self.STATE_QUERY_KEY_VARIANT.get(keyType.lower(), None)
            if variant is None:
                raise InternalError(
                    "query-state", f"{keyType} is not a known query-state key type"
                )
            return variant

        q = casper.StateQuery(key_variant=key_variant(keyType), key_base16=key)
        q.path_segments.extend(name for name in path.split("/") if name)
        return self.casperService.GetBlockState(
            casper.GetBlockStateRequest(block_hash_base16=blockHash, query=q)
        )

    @api
    def balance(self, address: str, block_hash: str):
        value = self.queryState(block_hash, address, "", "address")
        account = None
        try:
            account = value.account
        except AttributeError:
            return InternalError(
                "balance", f"Expected Account type value under {address}."
            )

        urefs = [u for u in account.known_urefs if u.name == "mint"]
        if len(urefs) == 0:
            raise InternalError(
                "balance",
                "Account's known_urefs map did not contain Mint contract address.",
            )

        mintPublic = urefs[0]
        mintPrivate = self.queryState(
            block_hash, mintPublic.key.uref.uref.hex(), "", "uref"
        )

        mintPrivateHex = mintPrivate.key.uref.uref.hex()
        purseAddrHex = ABI.byte_array(account.purse_id.uref).hex()
        localKeyValue = f"{mintPrivateHex}:{purseAddrHex}"

        balanceURef = self.queryState(block_hash, localKeyValue, "", "local")
        balance = self.queryState(
            block_hash, balanceURef.key.uref.uref.hex(), "", "uref"
        )
        return int(balance.big_int.value)

    @api
    def showDeploy(self, deploy_hash_base16: str, full_view=True):
        """
        Retrieve information about a single deploy by hash.
        """
        return self.casperService.GetDeployInfo(
            casper.GetDeployInfoRequest(
                deploy_hash_base16=deploy_hash_base16,
                view=(
                    full_view
                    and info.DeployInfo.View.FULL
                    or info.DeployInfo.View.BASIC
                ),
            )
        )

    @api
    def showDeploys(self, block_hash_base16: str, full_view=True):
        """
        Get the processed deploys within a block.
        """
        yield from self.casperService.StreamBlockDeploys_stream(
            casper.StreamBlockDeploysRequest(
                block_hash_base16=block_hash_base16,
                view=(
                    full_view
                    and info.DeployInfo.View.FULL
                    or info.DeployInfo.View.BASIC
                ),
            )
        )


def guarded_command(function):
    """
    Decorator of functions that implement CLI commands.

    Occasionally the node can throw some exceptions instead of properly sending us a response,
    those will be deserialized on our end and rethrown by the gRPC layer.
    In this case we want to catch the exception and return a non-zero return code to the shell.

    :param function:  function to be decorated
    :return:
    """

    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        try:
            rc = function(*args, **kwargs)
            # Generally the CLI commands are assumed to succeed if they don't throw,
            # but they can also return a positive error code if they need to.
            if rc is not None:
                return rc
            return 0
        except Exception as e:
            print(str(e), file=sys.stderr)
            return 1

    return wrapper


def hexify(o):
    """
    Convert protobuf message to text format with cryptographic keys and signatures in base 16.
    """
    return google.protobuf.text_format.MessageToString(o)


def _show_blocks(response, element_name="block"):
    count = 0
    for block in response:
        print(f"------------- {element_name} {count} ---------------")
        print(hexify(block))
        print("-----------------------------------------------------\n")
        count += 1
    print("count:", count)


def _show_block(response):
    print(hexify(response))


@guarded_command
def no_command(casperlabs_client, args):
    print("You must provide a command. --help for documentation of commands.")
    return 1


@guarded_command
def deploy_command(casperlabs_client, args):
    from_addr = bytes.fromhex(getattr(args, "from"))
    if len(from_addr) != 32:
        raise Exception(
            "--from must be 32 bytes encoded as 64 characters long hexadecimal"
        )

    kwargs = dict(
        from_addr=from_addr,
        gas_price=args.gas_price,
        payment=args.payment or args.session,
        session=args.session,
        public_key=args.public_key or None,
        private_key=args.private_key or None,
        session_args=args.session_args
        and ABI.args_from_json(args.session_args)
        or None,
        payment_args=args.payment_args
        and ABI.args_from_json(args.payment_args)
        or None,
        payment_hash=args.payment_hash and bytes.fromhex(args.payment_hash),
        payment_name=args.payment_name,
        payment_uref=args.payment_uref and bytes.fromhex(args.payment_uref),
        session_hash=args.session_hash and bytes.fromhex(args.session_hash),
        session_name=args.session_name,
        session_uref=args.session_uref and bytes.fromhex(args.session_uref),
    )
    _, deploy_hash = casperlabs_client.deploy(**kwargs)
    print(f"Success! Deploy {deploy_hash.hex()} deployed")


@guarded_command
def propose_command(casperlabs_client, args):
    response = casperlabs_client.propose()
    print(f"Success! Block hash: {response.block_hash.hex()}")


@guarded_command
def show_block_command(casperlabs_client, args):
    response = casperlabs_client.showBlock(args.hash, full_view=True)
    return _show_block(response)


@guarded_command
def show_blocks_command(casperlabs_client, args):
    response = casperlabs_client.showBlocks(args.depth)
    _show_blocks(response)


@guarded_command
def vdag_command(casperlabs_client, args):
    response = casperlabs_client.visualizeDag(args.depth)
    # TODO: call Graphviz
    print(hexify(response))


@guarded_command
def query_state_command(casperlabs_client, args):
    response = casperlabs_client.queryState(
        args.block_hash, args.key, args.path, getattr(args, "type")
    )
    print(hexify(response))


@guarded_command
def balance_command(casperlabs_client, args):
    response = casperlabs_client.balance(args.address, args.block_hash)
    print(response)


@guarded_command
def show_deploy_command(casperlabs_client, args):
    response = casperlabs_client.showDeploy(args.hash, full_view=False)
    print(hexify(response))


@guarded_command
def show_deploys_command(casperlabs_client, args):
    response = casperlabs_client.showDeploys(args.hash, full_view=False)
    _show_blocks(response, element_name="deploy")


def main():
    """
    Parse command line and call an appropriate command.
    """

    class Parser:
        def __init__(self):
            self.parser = argparse.ArgumentParser(add_help=False)
            self.parser.add_argument(
                "--help",
                action="help",
                default=argparse.SUPPRESS,
                help="show this help message and exit",
            )
            self.parser.add_argument(
                "-h",
                "--host",
                required=False,
                default=DEFAULT_HOST,
                type=str,
                help="Hostname or IP of node on which gRPC service is running.",
            )
            self.parser.add_argument(
                "-p",
                "--port",
                required=False,
                default=DEFAULT_PORT,
                type=int,
                help="Port used for external gRPC API.",
            )
            self.parser.add_argument(
                "--internal-port",
                required=False,
                default=DEFAULT_INTERNAL_PORT,
                type=int,
                help="Port used for internal gRPC API.",
            )
            self.parser.add_argument(
                "--node-id",
                required=False,
                type=str,
                help="node_id parameter for TLS connection",
            )
            self.parser.add_argument(
                "--certificate-file",
                required=False,
                type=str,
                help="Certificate file for TLS connection",
            )
            self.sp = self.parser.add_subparsers(help="Choose a request")

            self.parser.set_defaults(function=no_command)

        def addCommand(self, command: str, function, help, arguments):
            command_parser = self.sp.add_parser(command, help=help)
            command_parser.set_defaults(function=function)
            for (args, options) in arguments:
                command_parser.add_argument(*args, **options)

        def run(self):
            if len(sys.argv) < 2:
                self.parser.print_usage()
                return 1

            args = self.parser.parse_args()
            return args.function(
                CasperLabsClient(
                    args.host,
                    args.port,
                    args.internal_port,
                    args.node_id,
                    args.certificate_file,
                ),
                args,
            )

    parser = Parser()

    # fmt: off
    parser.addCommand('deploy', deploy_command, 'Deploy a smart contract source file to Casper on an existing running node. The deploy will be packaged and sent as a block to the network depending on the configuration of the Casper instance',
                      [[('-f', '--from'), dict(required=True, type=str, help="The public key of the account which is the context of this deployment, base16 encoded.")],
                       [('--gas-price',), dict(required=False, type=int, default=10, help='The price of gas for this transaction in units dust/gas. Must be positive integer.')],
                       [('-p', '--payment'), dict(required=False, type=str, default=None, help='Path to the file with payment code, by default fallbacks to the --session code')],
                       [('--payment-hash',), dict(required=False, type=str, default=None, help='Hash of the stored contract to be called in the payment; base16 encoded')],
                       [('--payment-name',), dict(required=False, type=str, default=None, help='Name of the stored contract (associated with the executing account) to be called in the payment')],
                       [('--payment-uref',), dict(required=False, type=str, default=None, help='URef of the stored contract to be called in the payment; base16 encoded')],
                       [('-s', '--session'), dict(required=False, type=str, default=None, help='Path to the file with session code')],
                       [('--session-hash',), dict(required=False, type=str, default=None, help='Hash of the stored contract to be called in the session; base16 encoded')],
                       [('--session-name',), dict(required=False, type=str, default=None, help='Name of the stored contract (associated with the executing account) to be called in the session')],
                       [('--session-uref',), dict(required=False, type=str, default=None, help='URef of the stored contract to be called in the session; base16 encoded')],
                       [('--session-args',), dict(required=False, type=str, help='JSON encoded list of session args, e.g.: [{"u32":1024},{"u64":12}]')],
                       [('--payment-args',), dict(required=False, type=str, help="""JSON encoded list of payment args, e.g.: [{"u512":100000}]""")],
                       [('--private-key',), dict(required=True, type=str, help='Path to the file with account public key (Ed25519)')],
                       [('--public-key',), dict(required=True, type=str, help='Path to the file with account private key (Ed25519)')]])

    parser.addCommand('propose', propose_command, 'Force a node to propose a block based on its accumulated deploys.', [])

    parser.addCommand('show-block', show_block_command, 'View properties of a block known by Casper on an existing running node. Output includes: parent hashes, storage contents of the tuplespace.',
                      [[('hash',), dict(type=str, help='the hash value of the block')]])

    parser.addCommand('show-blocks', show_blocks_command, 'View list of blocks in the current Casper view on an existing running node.',
                      [[('-d', '--depth'), dict(required=True, type=int, help='depth in terms of block height')]])

    parser.addCommand('show-deploy', show_deploy_command, 'View properties of a deploy known by Casper on an existing running node.',
                      [[('hash',), dict(type=str, help='Value of the deploy hash, base16 encoded.')]])

    parser.addCommand('show-deploys', show_deploys_command, 'View deploys included in a block.',
                      [[('hash',), dict(type=str, help='Value of the block hash, base16 encoded.')]])

    parser.addCommand('vdag', vdag_command, 'DAG in DOT format',
                      [[('-d', '--depth'), dict(required=True, type=int, help='depth in terms of block height')],
                       [('-o', '--out'), dict(required=False, type=str, help='output image filename, outputs to stdout if not specified, must end with one of the png, svg, svg_standalone, xdot, plain, plain_ext, ps, ps2, json, json0')],
                       [('-s', '--show-justification-lines'), dict(action='store_true', help='if justification lines should be shown')],
                       [('--stream',), dict(required=False, choices=('single-output', 'multiple-outputs'), help="subscribe to changes, '--out' has to be specified, valid values are 'single-output', 'multiple-outputs'")]])

    parser.addCommand('query-state', query_state_command, 'Query a value in the global state.',
                      [[('-b', '--block-hash'), dict(required=True, type=str, help='Hash of the block to query the state of')],
                       [('-k', '--key'), dict(required=True, type=str, help='Base16 encoding of the base key')],
                       [('-p', '--path'), dict(required=True, type=str, help="Path to the value to query. Must be of the form 'key1/key2/.../keyn'")],
                       [('-t', '--type'), dict(required=True, choices=('hash', 'uref', 'address', 'local'),
                                               help="Type of base key. Must be one of 'hash', 'uref', 'address' or 'local'. For 'local' key type, 'key' value format is {seed}:{rest}, where both parts are hex encoded.")]])

    parser.addCommand('balance', balance_command, 'Returns the balance of the account at the specified block.',
                      [[('-a', '--address'), dict(required=True, type=str, help="Account's public key in hex.")],
                       [('-b', '--block-hash'), dict(required=True, type=str, help='Hash of the block to query the state of')]])
    # fmt:on
    sys.exit(parser.run())


if __name__ == "__main__":
    main()
