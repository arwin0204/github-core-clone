#!/usr/bin/env python3
"""github-core-clone: a standalone, GitHub-compatible git forge in one process.

Python 3.13 standard library only. Serves, from its own in-memory store:

  * git smart HTTP  (/owner/repo.git): upload-pack protocol v2, receive-pack v0
  * REST v3         (/api/v3): repositories, branches, commits, contents, issues,
                     labels, comments, pull requests and merges
  * GraphQL v4     (/api/graphql): the documents gh and github-mcp-server send
  * MCP            (/mcp): JSON-RPC 2.0 over Streamable HTTP, revision 2026-07-28
                     (plus the legacy initialize handshake)

Lifecycle (the harness's, not the contest's): bind an ephemeral port, POST
{"port": N} to $MAKEFUN_ENV_URL/register, then poll GET $MAKEFUN_ENV_URL/done
until {"done": true} and exit 0. Without MAKEFUN_ENV_URL it just serves on
$FORGE_PORT (or an ephemeral port) forever, for local testing.

Determinism: every timestamp the forge emits is a fixed constant, every id is
assigned in creation order, and no wall clock or unseeded randomness reaches
any response. The viewer login is derived from the suite's own fixed scenario
seed (wire.ts SCENARIO_SEED), so it is identical on every trial.
"""

from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

ZERO_OID = "0" * 40
FIXED_TS = "2025-01-01T00:00:00Z"          # every created_at/updated_at/merged_at
FIXED_EPOCH = 1_700_000_000                # author/committer time of forge commits
VERSION = "1.0.0"  # the forge's version string
SERVER_NAME = "github-core-clone"
MCP_REVISION = "2026-07-28"
MCP_LEGACY_REVISIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
AGENT = "github-core-clone/1.0"

HEX40 = re.compile(r"^[0-9a-f]{40}$")

# ---------------------------------------------------------------------------
# the scenario's viewer (ported from kit sdk/conformance/wire.ts, fixed seed)
# ---------------------------------------------------------------------------

SCENARIO_SEED = 0x6A09E667F3BCC909
_U64 = (1 << 64) - 1
_FIRST_NAMES = (
    "ada", "bram", "cora", "dmitri", "elena", "felix", "greta", "hugo", "iris",
    "jonas", "kira", "lars", "mira", "nico", "olga", "pavel", "quinn", "rosa",
    "sven", "tess", "ulla", "vito", "wren", "yara", "zane",
)
_LAST_NAMES = (
    "anders", "bauer", "costa", "dahl", "eriksen", "fischer", "garcia", "holm",
    "iversen", "jansen", "keller", "lind", "moreau", "novak", "olsen", "peters",
    "quist", "rossi", "sato", "turi", "ulrich", "vega", "weber", "xu", "yilmaz",
    "zimmer",
)


def _fnv1a64(text: str) -> int:
    h = 0xCBF29CE484222325
    for ch in text:
        h ^= ord(ch)
        h = (h * 0x100000001B3) & _U64
    return h


def _mix64(value: int) -> int:
    z = (value + 0x9E3779B97F4A7C15) & _U64
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _U64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _U64
    return (z ^ (z >> 31)) & _U64


class _Rng:
    """xorshift64*, exactly as wire.ts."""

    def __init__(self, seed: int):
        masked = seed & _U64
        self._state = masked if masked else SCENARIO_SEED

    def next_u64(self) -> int:
        x = self._state
        x = (x ^ (x >> 12)) & _U64
        x = (x ^ ((x << 25) & _U64)) & _U64
        x = (x ^ (x >> 27)) & _U64
        self._state = x
        return (x * 0x2545F4914F6CDD1D) & _U64

    def next_u32(self) -> int:
        return self.next_u64() >> 32

    def next_int(self, n: int) -> int:
        if n == 1:
            return 0
        limit = 4_294_967_296 - (4_294_967_296 % n)
        while True:
            v = self.next_u32()
            if v < limit:
                return v % n

    def pick(self, items):
        return items[self.next_int(len(items))]


def _scenario_users(count: int):
    rng = _Rng(_mix64(SCENARIO_SEED ^ _fnv1a64("users")))
    used: set[str] = set()
    users = []
    for i in range(count):
        first = rng.pick(_FIRST_NAMES)
        last = rng.pick(_LAST_NAMES)
        base = re.sub(r"[^a-z0-9-]", "", f"{first}{last}")
        login = base
        suffix = 2
        while login in used:
            login = f"{base}-{suffix}"
            suffix += 1
        used.add(login)
        users.append({
            "id": 1000 + i,
            "login": login,
            "name": f"{first} {last}",
            "email": f"{login}@example.invalid",
        })
    return users


VIEWER = _scenario_users(12)[0]  # {"login": "adaiversen", ...}

# ---------------------------------------------------------------------------
# git object model
# ---------------------------------------------------------------------------

TYPE_CODE = {"commit": 1, "tree": 2, "blob": 3, "tag": 4}
CODE_TYPE = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}


def hash_object(otype: str, data: bytes) -> str:
    h = hashlib.sha1()
    h.update(f"{otype} {len(data)}\0".encode("utf-8"))
    h.update(data)
    return h.hexdigest()


def parse_tree(data: bytes):
    """[(mode, name, oid)] in stored order."""
    entries = []
    off = 0
    n = len(data)
    while off < n:
        sp = data.index(b" ", off)
        nul = data.index(b"\0", sp + 1)
        mode = data[off:sp].decode("ascii")
        name = data[sp + 1:nul].decode("utf-8")
        oid = data[nul + 1:nul + 21].hex()
        entries.append((mode, name, oid))
        off = nul + 21
    return entries


def _tree_sort_key(entry):
    mode, name, _ = entry
    key = f"{name}/" if mode == "40000" else name
    return key.encode("utf-8")


def build_tree(entries) -> bytes:
    out = bytearray()
    for mode, name, oid in sorted(entries, key=_tree_sort_key):
        out += f"{mode} {name}\0".encode("utf-8")
        out += bytes.fromhex(oid)
    return bytes(out)


def parse_commit(data: bytes):
    """(tree, parents, author, committer, message)."""
    text = data.decode("utf-8", "replace")
    head, _, message = text.partition("\n\n")
    tree = ""
    parents = []
    author = ""
    committer = ""
    for line in head.split("\n"):
        if line.startswith("tree "):
            tree = line[5:]
        elif line.startswith("parent "):
            parents.append(line[7:])
        elif line.startswith("author "):
            author = line[7:]
        elif line.startswith("committer "):
            committer = line[10:]
    return tree, parents, author, committer, message


def parse_tag(data: bytes):
    text = data.decode("utf-8", "replace")
    head, _, message = text.partition("\n\n")
    fields = {}
    for line in head.split("\n"):
        if " " in line:
            k, v = line.split(" ", 1)
            fields[k] = v
    return fields.get("object", ""), fields.get("type", ""), fields.get("tag", ""), fields.get("tagger", ""), message


def build_commit(tree: str, parents, message: str, who: str) -> bytes:
    lines = [f"tree {tree}"]
    for p in parents:
        lines.append(f"parent {p}")
    lines.append(f"author {who}")
    lines.append(f"committer {who}")
    return ("\n".join(lines) + "\n\n" + message).encode("utf-8")


def build_tag(obj: str, otype: str, tag: str, message: str, tagger: str) -> bytes:
    return (f"object {obj}\ntype {otype}\ntag {tag}\ntagger {tagger}\n\n{message}").encode("utf-8")


# ---------------------------------------------------------------------------
# pkt-line
# ---------------------------------------------------------------------------

FLUSH = b"0000"
DELIM = b"0001"


def pkt(payload: bytes) -> bytes:
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return f"{len(payload) + 4:04x}".encode("ascii") + payload


def pkt_lines(items) -> bytes:
    out = bytearray()
    for item in items:
        if item == "flush":
            out += FLUSH
        elif item == "delim":
            out += DELIM
        else:
            out += pkt(item)
    return bytes(out)


def parse_pkt_lines(data: bytes):
    """-> (packets, rest_offset); packet = ('data', bytes) | ('flush',) | ('delim',) | ('response-end',)."""
    packets = []
    off = 0
    n = len(data)
    while off + 4 <= n:
        head = data[off:off + 4]
        try:
            length = int(head.decode("ascii"), 16)
        except ValueError:
            break
        if length == 0:
            packets.append(("flush", b""))
            off += 4
        elif length == 1:
            packets.append(("delim", b""))
            off += 4
        elif length == 2:
            packets.append(("response-end", b""))
            off += 4
        elif length < 4 or off + length > n:
            break
        else:
            packets.append(("data", data[off + 4:off + length]))
            off += length
    return packets, off


# ---------------------------------------------------------------------------
# packfile
# ---------------------------------------------------------------------------

def _read_varint(buf: bytes, off: int):
    first = buf[off]
    size = first & 0x0F
    shift = 4
    off += 1
    byte = first
    while byte & 0x80:
        byte = buf[off]
        off += 1
        size += (byte & 0x7F) << shift
        shift += 7
    return size, off, first


def _read_ofs_base(buf: bytes, off: int):
    byte = buf[off]
    off += 1
    value = byte & 0x7F
    while byte & 0x80:
        byte = buf[off]
        off += 1
        value = ((value + 1) << 7) + (byte & 0x7F)
    return value, off


def apply_delta(base: bytes, delta: bytes) -> bytes:
    off = 0

    def read_size():
        nonlocal off
        value = 0
        shift = 0
        while True:
            byte = delta[off]
            off += 1
            value += (byte & 0x7F) << shift
            shift += 7
            if not (byte & 0x80):
                return value

    src_size = read_size()
    tgt_size = read_size()
    if src_size != len(base):
        raise ValueError("delta base size mismatch")
    out = bytearray()
    while off < len(delta):
        op = delta[off]
        off += 1
        if op & 0x80:
            cp_off = 0
            cp_size = 0
            for bit in range(4):
                if op & (1 << bit):
                    cp_off |= delta[off] << (bit * 8)
                    off += 1
            for bit in range(3):
                if op & (0x10 << bit):
                    cp_size |= delta[off] << (bit * 8)
                    off += 1
            if cp_size == 0:
                cp_size = 0x10000
            out += base[cp_off:cp_off + cp_size]
        elif op:
            out += delta[off:off + op]
            off += op
        else:
            raise ValueError("delta opcode 0")
    if len(out) != tgt_size:
        raise ValueError("delta size mismatch")
    return bytes(out)


def read_pack(data: bytes):
    """-> list of (type, bytes); raises ValueError on any corruption."""
    if len(data) < 32 or data[:4] != b"PACK":
        raise ValueError("not a packfile")
    version = int.from_bytes(data[4:8], "big")
    if version not in (2, 3):
        raise ValueError(f"unsupported pack version {version}")
    count = int.from_bytes(data[8:12], "big")
    trailer = data[-20:]
    if hashlib.sha1(data[:-20]).digest() != trailer:
        raise ValueError("pack trailer checksum mismatch")
    end = len(data) - 20
    raw = []  # (offset, type_code, size, data, base_offset|base_oid|None)
    by_offset = {}
    off = 12
    for _ in range(count):
        entry_off = off
        size, off, first = _read_varint(data, off)
        tcode = (first >> 4) & 0x07
        base = None
        if tcode == 6:
            dist, off = _read_ofs_base(data, off)
            base = entry_off - dist
        elif tcode == 7:
            base = data[off:off + 20].hex()
            off += 20
        elif tcode not in CODE_TYPE:
            raise ValueError(f"unknown pack type {tcode}")
        dec = zlib.decompressobj()
        chunk = dec.decompress(data[off:end])
        consumed = len(data[off:end]) - len(dec.unused_data)
        off += consumed
        if len(chunk) != size:
            raise ValueError("pack entry size mismatch")
        raw.append((entry_off, tcode, size, chunk, base))
        by_offset[entry_off] = len(raw) - 1
    if off != end:
        raise ValueError("pack has trailing bytes")

    resolved = {}

    def resolve(idx):
        if idx in resolved:
            return resolved[idx]
        entry_off, tcode, size, chunk, base = raw[idx]
        if tcode == 6:
            btype, bdata = resolve(by_offset[base])
            out = (btype, apply_delta(bdata, chunk))
        elif tcode == 7:
            # base must be an already-resolved object id in this pack
            found = None
            for j in range(len(raw)):
                if j in resolved and hash_object(*resolved[j]) == base:
                    found = resolved[j]
                    break
            if found is None:
                raise ValueError("ref-delta base not in pack")
            out = (found[0], apply_delta(found[1], chunk))
        else:
            out = (CODE_TYPE[tcode], chunk)
        resolved[idx] = out
        return out

    return [resolve(i) for i in range(len(raw))]


def _pack_object_header(otype: str, size: int) -> bytes:
    code = TYPE_CODE[otype]
    out = bytearray()
    first = (code << 4) | (size & 0x0F)
    size >>= 4
    while size:
        out.append(first | 0x80)
        first = size & 0x7F
        size >>= 7
    out.append(first)
    return bytes(out)


def write_pack(objects) -> bytes:
    """objects: iterable of (type, bytes); plain objects only, no deltas."""
    items = list(objects)
    parts = [b"PACK" + (2).to_bytes(4, "big") + len(items).to_bytes(4, "big")]
    for otype, data in items:
        parts.append(_pack_object_header(otype, len(data)))
        parts.append(zlib.compress(data))
    body = b"".join(parts)
    return body + hashlib.sha1(body).digest()


def pack_checksum_ok(data: bytes) -> bool:
    return len(data) >= 20 and hashlib.sha1(data[:-20]).digest() == data[-20:]

# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------

class Issue:
    __slots__ = ("number", "id", "title", "body", "state", "labels", "seq",
                 "comments", "closed_at")

    def __init__(self, number, gid, title, body, seq):
        self.number = number
        self.id = gid
        self.title = title
        self.body = body
        self.state = "open"
        self.labels = []
        self.seq = seq
        self.comments = []  # comment ids
        self.closed_at = None


class Comment:
    __slots__ = ("id", "issue_number", "body", "seq")

    def __init__(self, cid, issue_number, body, seq):
        self.id = cid
        self.issue_number = issue_number
        self.body = body
        self.seq = seq


class Pull:
    __slots__ = ("number", "id", "title", "body", "state", "head", "base",
                 "head_sha", "base_sha", "merged", "merged_at",
                 "merge_commit_sha", "seq", "merge_base")

    def __init__(self, number, gid, title, body, head, base, head_sha,
                 base_sha, seq, merge_base):
        self.number = number
        self.id = gid
        self.title = title
        self.body = body
        self.state = "open"
        self.head = head
        self.base = base
        self.head_sha = head_sha
        self.base_sha = base_sha
        self.merged = False
        self.merged_at = None
        self.merge_commit_sha = None
        self.seq = seq
        self.merge_base = merge_base


class Repo:
    __slots__ = ("owner", "name", "id", "description", "private",
                 "default_branch", "seq", "refs", "issues", "pulls",
                 "labels", "comments", "next_number", "next_comment")

    def __init__(self, owner, name, rid, description, private,
                 default_branch, seq):
        self.owner = owner
        self.name = name
        self.id = rid
        self.description = description
        self.private = private
        self.default_branch = default_branch
        self.seq = seq
        self.refs = {}            # "refs/heads/main" -> oid
        self.issues = {}          # number -> Issue
        self.pulls = {}           # number -> Pull
        self.labels = {}          # name -> {"name","color","description"}
        self.comments = {}        # comment id -> Comment
        self.next_number = 1      # shared issue/PR counter
        self.next_comment = 1


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.objects = {}         # oid -> (type, bytes)
        self.repos = {}           # (owner, name) -> Repo
        self.next_repo_id = 1
        self.next_issue_gid = 1
        self.next_comment_gid = 1
        self.next_seq = 1
        self.req_counter = 0

    def next_request_id(self) -> str:
        with self.lock:
            self.req_counter += 1
            return f"req-{self.req_counter:08d}"

    def put_object(self, otype: str, data: bytes) -> str:
        oid = hash_object(otype, data)
        if oid not in self.objects:
            self.objects[oid] = (otype, data)
        return oid

    def get_object(self, oid: str):
        return self.objects.get(oid)

    # -- reachability -------------------------------------------------------

    def collect_reachable(self, roots) -> set:
        seen = set()
        stack = [r for r in roots if isinstance(r, str) and HEX40.match(r or "")]
        while stack:
            oid = stack.pop()
            if oid in seen:
                continue
            obj = self.objects.get(oid)
            if obj is None:
                continue
            seen.add(oid)
            otype, data = obj
            if otype == "commit":
                tree, parents, _, _, _ = parse_commit(data)
                stack.append(tree)
                stack.extend(parents)
            elif otype == "tag":
                target, _, _, _, _ = parse_tag(data)
                stack.append(target)
            elif otype == "tree":
                for mode, _name, child in parse_tree(data):
                    if mode != "160000":
                        stack.append(child)
        return seen

    def is_ancestor(self, anc: str, oid: str) -> bool:
        """True when `anc` is reachable from `oid` (or equal)."""
        if anc == oid:
            return True
        seen = set()
        stack = [oid]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            obj = self.objects.get(cur)
            if obj is None or obj[0] not in ("commit", "tag"):
                continue
            if obj[0] == "tag":
                target, _, _, _, _ = parse_tag(obj[1])
                if target == anc:
                    return True
                stack.append(target)
            else:
                _, parents, _, _, _ = parse_commit(obj[1])
                if anc in parents:
                    return True
                stack.extend(parents)
        return False

    def peel_to_commit(self, oid: str):
        """Follow tag objects to the commit they point at (or None)."""
        cur = oid
        for _ in range(16):
            obj = self.objects.get(cur)
            if obj is None:
                return None
            if obj[0] == "tag":
                target, _, _, _, _ = parse_tag(obj[1])
                cur = target
            elif obj[0] == "commit":
                return cur
            else:
                return None
        return None

    def commit_tree(self, commit_oid: str):
        obj = self.objects.get(commit_oid)
        if obj is None or obj[0] != "commit":
            return None
        tree, _, _, _, _ = parse_commit(obj[1])
        return tree

    # -- trees --------------------------------------------------------------

    def files_of_tree(self, tree_oid: str, prefix=""):
        """{path: (mode, oid)} for every blob under a tree (recursive)."""
        out = {}
        obj = self.objects.get(tree_oid)
        if obj is None or obj[0] != "tree":
            return out
        for mode, name, oid in parse_tree(obj[1]):
            path = f"{prefix}{name}"
            if mode == "40000":
                out.update(self.files_of_tree(oid, path + "/"))
            else:
                out[path] = (mode, oid)
        return out

    def build_tree_from_files(self, files):
        """files: {path: (mode, bytes)} -> root tree oid (creates objects)."""
        # nested dict: name -> ("file", mode, bytes) | ("dir", {...})
        root: dict = {}
        for path, (mode, data) in files.items():
            parts = path.split("/")
            node = root
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = (mode, data)

        def build(node) -> str:
            entries = []
            for name, value in node.items():
                if isinstance(value, dict):
                    entries.append(("40000", name, build(value)))
                else:
                    mode, data = value
                    entries.append((mode, name, self.put_object("blob", data)))
            return self.put_object("tree", build_tree(entries))

        return build(root)

    def files_of_commit(self, commit_oid: str):
        tree = self.commit_tree(commit_oid)
        if tree is None:
            return {}
        return self.files_of_tree(tree)


STORE = Store()


# ---------------------------------------------------------------------------
# git fetch computation
# ---------------------------------------------------------------------------

def _have_closure(store: Store, haves, shallow):
    """Objects the client already holds: everything reachable from `haves`,
    except that a shallow boundary commit contributes only itself and its
    tree — never its parents (a shallow clone does not hold them)."""
    boundaries = set(shallow or ())
    seen = set()
    stack = [h for h in haves if isinstance(h, str) and HEX40.match(h or "")]
    while stack:
        oid = stack.pop()
        if oid in seen:
            continue
        obj = store.objects.get(oid)
        if obj is None:
            continue
        seen.add(oid)
        otype, data = obj
        if otype == "commit":
            tree, parents, _, _, _ = parse_commit(data)
            stack.append(tree)
            if oid not in boundaries:
                stack.extend(parents)
        elif otype == "tag":
            target, _, _, _, _ = parse_tag(data)
            stack.append(target)
        elif otype == "tree":
            for mode, _name, child in parse_tree(data):
                if mode != "160000":
                    stack.append(child)
    return seen


def compute_fetch(store: Store, wants, haves, depth, shallow):
    """-> (objects {oid: (type, data)}, new_shallow [oid], unshallow [oid]).

    Depth applies to commits only (1 = the wanted commits themselves); trees
    and blobs of an included commit are always complete. Objects reachable
    from `haves` are never re-sent.
    """
    have_reach = _have_closure(store, haves, shallow)
    # BFS over commits from the wants, peeling tags first.
    dist = {}
    queue = []
    for want in wants:
        peeled = store.peel_to_commit(want)
        if peeled is not None:
            if peeled not in dist:
                dist[peeled] = 1
                queue.append(peeled)
        elif want in store.objects:
            # a non-commit want (blob/tree/tag target): include directly
            dist[want] = 0
            queue.append(want)
    head = 0
    while head < len(queue):
        cur = queue[head]
        head += 1
        d = dist[cur]
        obj = store.objects.get(cur)
        if obj is None or obj[0] != "commit":
            continue
        if depth is not None and d >= depth:
            continue
        if depth is None and cur in (shallow or ()):
            continue  # a shallow boundary: do not traverse past it
        _, parents, _, _, _ = parse_commit(obj[1])
        for parent in parents:
            if parent not in dist:
                dist[parent] = d + 1
                queue.append(parent)
    included = set(dist)
    if depth is not None:
        new_shallow = sorted(c for c in included if dist[c] == depth and store.objects.get(c, (None,))[0] == "commit")
        unshallow = sorted(b for b in (shallow or ()) if b in included and b not in new_shallow)
    elif shallow:
        new_shallow = sorted(b for b in shallow if b in included)
        unshallow = []
    else:
        new_shallow = []
        unshallow = []
    # objects: every included commit's closure through its tree only (no parents
    # beyond the depth cut), plus the wanted tag objects themselves.
    wanted_objs = set()
    for want in wants:
        if want in store.objects:
            wanted_objs.add(want)
    for commit_oid in included:
        obj = store.objects.get(commit_oid)
        if obj is None:
            continue
        if obj[0] == "commit":
            wanted_objs.add(commit_oid)
            tree = store.commit_tree(commit_oid)
            if tree:
                wanted_objs |= store.collect_reachable([tree])
        else:
            wanted_objs |= store.collect_reachable([commit_oid])
    send = {oid: store.objects[oid] for oid in wanted_objs
            if oid not in have_reach and oid in store.objects}
    return send, new_shallow, unshallow


# ---------------------------------------------------------------------------
# diffs and merges
# ---------------------------------------------------------------------------

def diff_files(base_files, head_files):
    """-> [{filename, status, additions, deletions, changes}] sorted by path."""
    out = []
    for path in sorted(set(base_files) | set(head_files)):
        in_base = path in base_files
        in_head = path in head_files
        if in_base and not in_head:
            old = _blob_text(base_files[path][1])
            out.append({"filename": path, "status": "removed",
                        "additions": 0, "deletions": _line_count(old),
                        "changes": _line_count(old)})
        elif in_head and not in_base:
            new = _blob_text(head_files[path][1])
            out.append({"filename": path, "status": "added",
                        "additions": _line_count(new), "deletions": 0,
                        "changes": _line_count(new)})
        else:
            old_oid = base_files[path][1]
            new_oid = head_files[path][1]
            if old_oid == new_oid:
                continue
            old = _blob_text(old_oid)
            new = _blob_text(new_oid)
            adds, dels = _diff_counts(old, new)
            out.append({"filename": path, "status": "modified",
                        "additions": adds, "deletions": dels,
                        "changes": adds + dels})
    return out


def _blob_text(oid: str) -> str:
    obj = STORE.objects.get(oid)
    if obj is None:
        return ""
    return obj[1].decode("utf-8", "replace")


def _line_count(text: str) -> int:
    if text == "":
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _diff_counts(old: str, new: str):
    adds = dels = 0
    for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm=""):
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            adds += 1
        elif line.startswith("-"):
            dels += 1
    return adds, dels


def merge_base(store: Store, head_oid: str, base_oid: str):
    """Nearest common ancestor of head and base (BFS from head)."""
    base_ancestors = store.collect_reachable([base_oid])
    queue = [head_oid]
    seen = set()
    while queue:
        cur = queue.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        if cur in base_ancestors:
            return cur
        obj = store.objects.get(cur)
        if obj is None or obj[0] != "commit":
            continue
        _, parents, _, _, _ = parse_commit(obj[1])
        queue.extend(parents)
    return None


def three_way_merge(store: Store, base_oid: str, ours_oid: str, theirs_oid: str):
    """-> (merged_files {path: (mode, bytes)}, conflict_paths [path]).

    merged_files holds the full resulting file map; a path whose three sides
    all differ is a conflict.
    """
    base_files = store.files_of_commit(base_oid) if base_oid else {}
    ours_files = store.files_of_commit(ours_oid) if ours_oid else {}
    theirs_files = store.files_of_commit(theirs_oid) if theirs_oid else {}
    result = {}
    conflicts = []
    for path in sorted(set(base_files) | set(ours_files) | set(theirs_files)):
        b = base_files.get(path)
        o = ours_files.get(path)
        t = theirs_files.get(path)
        b_oid = b[1] if b else None
        o_oid = o[1] if o else None
        t_oid = t[1] if t else None
        if o_oid == t_oid:
            if o is not None:
                result[path] = (o[0], STORE.objects[o_oid][1])
        elif b_oid == o_oid:
            if t is not None:
                result[path] = (t[0], STORE.objects[t_oid][1])
        elif b_oid == t_oid:
            if o is not None:
                result[path] = (o[0], STORE.objects[o_oid][1])
        else:
            conflicts.append(path)
    return result, conflicts


def forge_who() -> str:
    return f"{VIEWER['name']} <{VIEWER['email']}> {FIXED_EPOCH} +0000"


def make_commit(store: Store, tree_oid: str, parents, message: str) -> str:
    who = forge_who()
    data = build_commit(tree_oid, parents, message, who)
    return store.put_object("commit", data)

# ---------------------------------------------------------------------------
# global (GraphQL Relay) ids
# ---------------------------------------------------------------------------

def gid_encode(kind: str, *parts: str) -> str:
    raw = ":".join([kind, *parts]).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def gid_decode(value):
    """-> (kind, [parts]) or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        raw = base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")
    except Exception:
        return None
    bits = raw.split(":")
    if len(bits) < 2:
        return None
    return bits[0], bits[1:]


# ---------------------------------------------------------------------------
# REST views
# ---------------------------------------------------------------------------

def user_view(login: str, uid: int = 1000, name: str = ""):
    return {
        "login": login,
        "id": uid,
        "node_id": gid_encode("User", login),
        "avatar_url": "",
        "gravatar_id": "",
        "url": f"/api/v3/users/{login}",
        "html_url": f"https://example.invalid/{login}",
        "type": "User",
        "site_admin": False,
        "name": name or login,
        "email": f"{login}@example.invalid",
    }


def repo_view(repo: Repo, host: str):
    base = f"http://{host}/{repo.owner}/{repo.name}"
    return {
        "id": repo.id,
        "node_id": gid_encode("Repository", str(repo.id)),
        "name": repo.name,
        "full_name": f"{repo.owner}/{repo.name}",
        "owner": user_view(repo.owner, VIEWER["id"], VIEWER["name"]),
        "private": repo.private,
        "html_url": base,
        "description": repo.description,
        "fork": False,
        "url": f"/api/v3/repos/{repo.owner}/{repo.name}",
        "created_at": FIXED_TS,
        "updated_at": FIXED_TS,
        "pushed_at": FIXED_TS,
        "git_url": f"git://{host}/{repo.owner}/{repo.name}.git",
        "ssh_url": f"git@example.invalid:{repo.owner}/{repo.name}.git",
        "clone_url": f"{base}.git",
        "homepage": None,
        "size": 0,
        "stargazers_count": 0,
        "watchers_count": 0,
        "language": None,
        "has_issues": True,
        "has_projects": False,
        "has_downloads": True,
        "has_wiki": False,
        "has_pages": False,
        "has_discussions": False,
        "forks_count": 0,
        "archived": False,
        "disabled": False,
        "open_issues_count": sum(1 for i in repo.issues.values() if i.state == "open"),
        "license": None,
        "allow_forking": True,
        "is_template": False,
        "topics": [],
        "visibility": "private" if repo.private else "public",
        "default_branch": repo.default_branch,
        "permissions": {"admin": True, "maintain": True, "push": True,
                        "triage": True, "pull": True},
    }


def label_view(name: str, color: str = "ededed"):
    stable_id = int(hashlib.sha1(name.encode("utf-8")).hexdigest()[:12], 16) % 10**9 + 1
    return {"id": stable_id, "node_id": gid_encode("Label", name),
            "name": name, "color": color, "default": False, "description": ""}


def issue_view(repo: Repo, issue: Issue, host: str):
    return {
        "id": issue.id,
        "node_id": gid_encode("Issue", repo.owner, repo.name, str(issue.number)),
        "url": f"/api/v3/repos/{repo.owner}/{repo.name}/issues/{issue.number}",
        "repository_url": f"/api/v3/repos/{repo.owner}/{repo.name}",
        "html_url": f"http://{host}/{repo.owner}/{repo.name}/issues/{issue.number}",
        "number": issue.number,
        "title": issue.title,
        "body": issue.body,
        "user": user_view(repo.owner, VIEWER["id"], VIEWER["name"]),
        "labels": [label_view(n, repo.labels.get(n, {}).get("color", "ededed"))
                   for n in issue.labels],
        "state": issue.state,
        "locked": False,
        "assignee": None,
        "assignees": [],
        "comments": len(issue.comments),
        "created_at": FIXED_TS,
        "updated_at": FIXED_TS,
        "closed_at": issue.closed_at,
        "author_association": "OWNER",
        "state_reason": None,
    }


def comment_view(repo: Repo, comment: Comment, host: str):
    return {
        "id": comment.id,
        "node_id": gid_encode("IssueComment", repo.owner, repo.name,
                              str(comment.issue_number), str(comment.id)),
        "url": f"/api/v3/repos/{repo.owner}/{repo.name}/issues/comments/{comment.id}",
        "html_url": f"http://{host}/{repo.owner}/{repo.name}/issues/{comment.issue_number}#issuecomment-{comment.id}",
        "issue_url": f"/api/v3/repos/{repo.owner}/{repo.name}/issues/{comment.issue_number}",
        "user": user_view(repo.owner, VIEWER["id"], VIEWER["name"]),
        "body": comment.body,
        "created_at": FIXED_TS,
        "updated_at": FIXED_TS,
        "author_association": "OWNER",
    }


def pull_view(repo: Repo, pr: Pull, host: str):
    base = f"http://{host}/{repo.owner}/{repo.name}"
    return {
        "id": pr.id,
        "node_id": gid_encode("PullRequest", repo.owner, repo.name, str(pr.number)),
        "url": f"/api/v3/repos/{repo.owner}/{repo.name}/pulls/{pr.number}",
        "html_url": f"{base}/pull/{pr.number}",
        "number": pr.number,
        "state": pr.state,
        "locked": False,
        "title": pr.title,
        "body": pr.body,
        "user": user_view(repo.owner, VIEWER["id"], VIEWER["name"]),
        "created_at": FIXED_TS,
        "updated_at": FIXED_TS,
        "closed_at": pr.merged_at if pr.state == "closed" else None,
        "merged_at": pr.merged_at,
        "merge_commit_sha": pr.merge_commit_sha,
        "merged": pr.merged,
        "mergeable": None,
        "head": {"label": f"{repo.owner}:{pr.head}", "ref": pr.head,
                 "sha": pr.head_sha,
                 "user": user_view(repo.owner, VIEWER["id"], VIEWER["name"]),
                 "repo": repo_view(repo, host)},
        "base": {"label": f"{repo.owner}:{pr.base}", "ref": pr.base,
                 "sha": pr.base_sha,
                 "user": user_view(repo.owner, VIEWER["id"], VIEWER["name"]),
                 "repo": repo_view(repo, host)},
        "author_association": "OWNER",
        "draft": False,
    }


def get_repo_or_none(owner: str, name: str):
    if owner != VIEWER["login"]:
        return None
    return STORE.repos.get((owner, name))


def create_issue_in_repo(repo: Repo, title, body, labels):
    with STORE.lock:
        number = repo.next_number
        repo.next_number += 1
        gid = STORE.next_issue_gid
        STORE.next_issue_gid += 1
        seq = STORE.next_seq
        STORE.next_seq += 1
        issue = Issue(number, gid, title, body if body is not None else "", seq)
        issue.labels = list(labels)
        repo.issues[number] = issue
        return issue


def create_pull_in_repo(repo: Repo, title, body, head, base, head_sha,
                        base_sha, merge_base_oid):
    with STORE.lock:
        number = repo.next_number
        repo.next_number += 1
        gid = STORE.next_issue_gid
        STORE.next_issue_gid += 1
        seq = STORE.next_seq
        STORE.next_seq += 1
        pr = Pull(number, gid, title, body if body is not None else "",
                  head, base, head_sha, base_sha, seq, merge_base_oid)
        repo.pulls[number] = pr
        return pr


def pr_mergeable(store: Store, repo: Repo, pr: Pull) -> str:
    """MERGEABLE / CONFLICTING / UNKNOWN for an open pull request."""
    if pr.state != "open":
        return "UNKNOWN"
    base_tip = repo.refs.get(f"refs/heads/{pr.base}")
    head_tip = repo.refs.get(f"refs/heads/{pr.head}")
    if base_tip is None or head_tip is None:
        return "UNKNOWN"
    if base_tip == pr.base_sha and head_tip == pr.head_sha:
        mb = pr.merge_base
    else:
        mb = merge_base(store, head_tip, base_tip)
    if mb is None:
        return "UNKNOWN"
    _, conflicts = three_way_merge(store, mb, base_tip, head_tip)
    return "CONFLICTING" if conflicts else "MERGEABLE"


def do_merge(store: Store, repo: Repo, pr: Pull):
    """Squash-merge an open PR. -> (ok, sha|reason)."""
    base_ref = f"refs/heads/{pr.base}"
    head_ref = f"refs/heads/{pr.head}"
    base_tip = repo.refs.get(base_ref)
    head_tip = repo.refs.get(head_ref)
    if base_tip is None or head_tip is None:
        return False, "head or base branch is missing"
    mb = merge_base(store, head_tip, base_tip)
    if mb is None:
        return False, "no merge base"
    merged_files, conflicts = three_way_merge(store, mb, base_tip, head_tip)
    if conflicts:
        return False, "merge conflict"
    tree_oid = store.build_tree_from_files(merged_files)
    message = f"{pr.title} (#{pr.number})"
    sha = make_commit(store, tree_oid, [base_tip], message)
    repo.refs[base_ref] = sha
    pr.state = "closed"
    pr.merged = True
    pr.merged_at = FIXED_TS
    pr.merge_commit_sha = sha
    pr.base_sha = sha
    return True, sha

# ---------------------------------------------------------------------------
# git smart HTTP
# ---------------------------------------------------------------------------

UPLOAD_CAPS = ("side-band-64k thin-pack ofs-delta shallow multi_ack "
               "multi_ack_detailed agent=" + AGENT)
RECEIVE_CAPS = ("report-status report-status-v2 side-band-64k ofs-delta "
                "delete-refs agent=" + AGENT)


def _sorted_refs(repo: Repo):
    return sorted(repo.refs.items())


def _peel_line(repo: Repo, ref: str, oid: str):
    """Peeled line for an annotated tag, or None."""
    obj = STORE.objects.get(oid)
    if obj is None or obj[0] != "tag":
        return None
    peeled = STORE.peel_to_commit(oid)
    if peeled is None:
        return None
    return f"{peeled} {ref}^{{}}\n"


def git_advertisement(repo: Repo, service: str) -> bytes:
    """v0 advertisement for git-upload-pack / git-receive-pack."""
    default_ref = f"refs/heads/{repo.default_branch}"
    head_oid = repo.refs.get(default_ref, ZERO_OID)
    caps = UPLOAD_CAPS if service == "git-upload-pack" else RECEIVE_CAPS
    if service == "git-upload-pack":
        caps += f" symref=HEAD:{default_ref}"
    preamble = f"# service={service}\n"
    out = bytearray()
    out += pkt(preamble)
    out += FLUSH
    if not repo.refs:
        out += pkt(f"{ZERO_OID} capabilities^{{}}\0{caps}\n")
    else:
        out += pkt(f"{head_oid} HEAD\0{caps}\n")
        for ref, oid in _sorted_refs(repo):
            out += pkt(f"{oid} {ref}\n")
            peeled = _peel_line(repo, ref, oid)
            if peeled:
                out += pkt(peeled)
    out += FLUSH
    return bytes(out)


def git_v2_advertisement() -> bytes:
    return pkt_lines([
        "version 2\n",
        "ls-refs=unborn\n",
        "fetch=shallow filter\n",
        "object-format=sha1\n",
        f"agent={AGENT}\n",
        "flush",
    ])


def git_ls_refs(repo: Repo, args) -> bytes:
    """v2 ls-refs response. args: {peel, symrefs, unborn, prefixes}."""
    default_ref = f"refs/heads/{repo.default_branch}"
    lines = []
    if args["prefixes"]:
        # HEAD is excluded whenever any ref-prefix is given.
        for ref, oid in _sorted_refs(repo):
            if not any(ref.startswith(p) for p in args["prefixes"]):
                continue
            line = f"{oid} {ref}"
            if args["peel"]:
                peeled = STORE.peel_to_commit(oid) if _is_tag(oid) else None
                if peeled:
                    line += f" peeled:{peeled}"
            lines.append(line + "\n")
        return pkt_lines(lines + ["flush"])
    head_oid = repo.refs.get(default_ref)
    if head_oid is None:
        if args["unborn"]:
            lines.append(f"unborn HEAD symref-target:{default_ref}\n")
    else:
        line = f"{head_oid} HEAD"
        if args["symrefs"]:
            line += f" symref-target:{default_ref}"
        lines.append(line + "\n")
    for ref, oid in _sorted_refs(repo):
        line = f"{oid} {ref}"
        if args["peel"]:
            peeled = STORE.peel_to_commit(oid) if _is_tag(oid) else None
            if peeled:
                line += f" peeled:{peeled}"
        lines.append(line + "\n")
    return pkt_lines(lines + ["flush"])


def _is_tag(oid: str) -> bool:
    obj = STORE.objects.get(oid)
    return obj is not None and obj[0] == "tag"


def _sideband_pack(pack: bytes, chunk: int = 65515) -> bytes:
    """Pack bytes wrapped in sideband band-1 pkt-lines."""
    out = bytearray()
    for off in range(0, len(pack), chunk):
        out += pkt(b"\x01" + pack[off:off + chunk])
    return bytes(out)


def _ack_lines(store: Store, wants, haves, send_empty: bool):
    """ACK lines for haves common with the wanted history, else NAK."""
    want_reach = store.collect_reachable(wants)
    common = [h for h in haves if h in want_reach]
    if common:
        return [f"ACK {h}\n" for h in common]
    return ["NAK\n"]


def git_fetch_v2(repo: Repo, body: bytes) -> bytes:
    packets, _ = parse_pkt_lines(body)
    wants, haves, shallow = [], [], []
    depth = None
    for kind, payload in packets:
        if kind != "data":
            continue
        line = payload.decode("utf-8", "replace").strip()
        if line.startswith("want "):
            wants.append(line[5:].strip())
        elif line.startswith("have "):
            haves.append(line[5:].strip())
        elif line.startswith("shallow "):
            shallow.append(line[8:].strip())
        elif line.startswith("deepen "):
            try:
                depth = int(line[7:].strip())
            except ValueError:
                depth = None
    send, new_shallow, unshallow = compute_fetch(STORE, wants, haves, depth, shallow)
    out = bytearray()
    out += pkt("acknowledgments\n")
    if not send:
        out += pkt_lines(_ack_lines(STORE, wants, haves, True) + ["ready\n", "delim"])
    else:
        out += pkt_lines(_ack_lines(STORE, wants, haves, False) + ["delim"])
    if new_shallow or unshallow:
        lines = ["shallow-info\n"]
        lines += [f"shallow {o}\n" for o in new_shallow]
        lines += [f"unshallow {o}\n" for o in unshallow]
        out += pkt_lines(lines + ["delim"])
    if send:
        pack = write_pack([send[k] for k in sorted(send)])
        out += pkt("packfile\n")
        out += _sideband_pack(pack)
    out += FLUSH
    return bytes(out)


def git_fetch_v0(repo: Repo, body: bytes) -> bytes:
    packets, rest = parse_pkt_lines(body)
    wants, haves, shallow = [], [], []
    depth = None
    seen_flush = False
    for kind, payload in packets:
        if kind == "flush":
            seen_flush = True
            continue
        if kind != "data":
            continue
        line = payload.decode("utf-8", "replace").strip()
        if "\0" in line:
            line = line.split("\0", 1)[0]
        if not seen_flush:
            if line.startswith("want "):
                wants.append(line[5:].strip())
            elif line.startswith("shallow "):
                shallow.append(line[8:].strip())
            elif line.startswith("deepen "):
                try:
                    depth = int(line[7:].strip())
                except ValueError:
                    depth = None
        else:
            if line.startswith("have "):
                haves.append(line[5:].strip())
    send, new_shallow, unshallow = compute_fetch(STORE, wants, haves, depth, shallow)
    out = bytearray()
    if not send:
        for line in _ack_lines(STORE, wants, haves, True):
            out += pkt(line)
    else:
        for line in _ack_lines(STORE, wants, haves, False):
            out += pkt(line)
    for oid in new_shallow:
        out += pkt(f"shallow {oid}\n")
    for oid in unshallow:
        out += pkt(f"unshallow {oid}\n")
    if send:
        pack = write_pack([send[k] for k in sorted(send)])
        out += _sideband_pack(pack)
    out += FLUSH
    return bytes(out)


def git_receive_pack(repo: Repo, body: bytes) -> bytes:
    packets, rest_off = parse_pkt_lines(body)
    commands = []
    for kind, payload in packets:
        if kind == "flush":
            break
        if kind != "data":
            continue
        line = payload.decode("utf-8", "replace").strip()
        if "\0" in line:
            line = line.split("\0", 1)[0]
        parts = line.split(" ")
        if len(parts) == 3 and HEX40.match(parts[0]) and HEX40.match(parts[1]):
            commands.append((parts[0], parts[1], parts[2]))
    pack_bytes = body[rest_off:] if rest_off < len(body) else b""
    unpack_ok = True
    unpack_error = ""
    new_objects = []
    if pack_bytes:
        try:
            new_objects = read_pack(pack_bytes)
        except ValueError as exc:
            unpack_ok = False
            unpack_error = str(exc)
    results = []
    if unpack_ok:
        with STORE.lock:
            for otype, data in new_objects:
                STORE.put_object(otype, data)
            for old, new, ref in commands:
                if old == ZERO_OID and new == ZERO_OID:
                    results.append((ref, False, "invalid old/new"))
                    continue
                cur = repo.refs.get(ref)
                if old == ZERO_OID:
                    if cur is not None:
                        results.append((ref, False, "already exists"))
                        continue
                    if new not in STORE.objects:
                        results.append((ref, False, "object not found"))
                        continue
                    repo.refs[ref] = new
                    results.append((ref, True, ""))
                elif new == ZERO_OID:
                    if cur is None:
                        results.append((ref, False, "ref does not exist"))
                        continue
                    if cur != old:
                        results.append((ref, False, "non-fast-forward"))
                        continue
                    del repo.refs[ref]
                    results.append((ref, True, ""))
                else:
                    if cur is None:
                        results.append((ref, False, "ref does not exist"))
                        continue
                    if cur != old:
                        results.append((ref, False, "non-fast-forward"))
                        continue
                    if new not in STORE.objects:
                        results.append((ref, False, "object not found"))
                        continue
                    if not STORE.is_ancestor(old, new):
                        results.append((ref, False, "non-fast-forward"))
                        continue
                    repo.refs[ref] = new
                    results.append((ref, True, ""))
    else:
        results = [(ref, False, "unpack failed") for _, _, ref in commands]
    inner = bytearray()
    if unpack_ok:
        inner += pkt("unpack ok\n")
    else:
        inner += pkt(f"unpack {unpack_error or 'error'}\n")
    for ref, ok, reason in results:
        if ok:
            inner += pkt(f"ok {ref}\n")
        else:
            inner += pkt(f"ng {ref} {reason}\n")
    inner += FLUSH
    return pkt(b"\x01" + bytes(inner)) + FLUSH

# ---------------------------------------------------------------------------
# REST v3
# ---------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _page_params(query):
    try:
        per_page = int(query.get("per_page", ["30"])[0])
    except ValueError:
        per_page = 30
    per_page = max(1, min(100, per_page))
    try:
        page = int(query.get("page", ["1"])[0])
    except ValueError:
        page = 1
    page = max(1, page)
    return per_page, page


def _paginate(items, query, base_url):
    per_page, page = _page_params(query)
    total = len(items)
    start = (page - 1) * per_page
    chunk = items[start:start + per_page]
    links = []
    last_page = max(1, (total + per_page - 1) // per_page)
    def url(p):
        q = dict(query)
        q["per_page"] = [str(per_page)]
        q["page"] = [str(p)]
        return base_url + "?" + urllib.parse.urlencode(
            {k: v[0] for k, v in q.items()})
    if page < last_page:
        links.append(f'<{url(page + 1)}>; rel="next"')
    if page > 1:
        links.append(f'<{url(page - 1)}>; rel="prev"')
    if last_page > 1:
        links.append(f'<{url(1)}>; rel="first"')
        links.append(f'<{url(last_page)}>; rel="last"')
    return chunk, {"Link": ", ".join(links)} if links else {}


def _branch_tip(repo: Repo, branch: str):
    return repo.refs.get(f"refs/heads/{branch}")


def _commit_chain(store: Store, tip: str):
    """Commits reachable from tip, newest first (first-parent-first DFS)."""
    out = []
    seen = set()
    stack = [tip]
    while stack:
        cur = stack.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        obj = store.objects.get(cur)
        if obj is None or obj[0] != "commit":
            continue
        out.append(cur)
        _, parents, _, _, _ = parse_commit(obj[1])
        stack = parents + stack
    return out


def _contents_entry(repo: Repo, ref_tip, path: str, host: str):
    """File or directory entry for GET /contents, or None."""
    if ref_tip is None:
        return None
    files = STORE.files_of_commit(ref_tip)
    if path in files:
        mode, oid = files[path]
        data = STORE.objects[oid][1]
        return {
            "type": "file", "encoding": "base64", "size": len(data),
            "name": path.rsplit("/", 1)[-1], "path": path, "sha": oid,
            "content": base64.b64encode(data).decode("ascii"),
            "url": f"/api/v3/repos/{repo.owner}/{repo.name}/contents/{path}",
            "html_url": f"http://{host}/{repo.owner}/{repo.name}/blob/{repo.default_branch}/{path}",
            "git_url": f"/api/v3/repos/{repo.owner}/{repo.name}/git/blobs/{oid}",
            "download_url": None,
            "_links": {},
        }
    prefix = path.rstrip("/") + "/" if path else ""
    children = {}
    for fpath, (mode, oid) in files.items():
        if not fpath.startswith(prefix):
            continue
        rest = fpath[len(prefix):]
        if "/" in rest:
            name = rest.split("/", 1)[0]
            children[name] = {"type": "dir", "name": name,
                              "path": prefix + name, "sha": "",
                              "url": f"/api/v3/repos/{repo.owner}/{repo.name}/contents/{prefix}{name}"}
        else:
            children[rest] = {"type": "file", "name": rest, "path": fpath,
                              "sha": oid,
                              "url": f"/api/v3/repos/{repo.owner}/{repo.name}/contents/{fpath}"}
    if children:
        return [children[k] for k in sorted(children)]
    return None


def handle_rest(method: str, seg, query, body, host: str, base_url: str):
    """-> (status, extra_headers, json_obj|None). Raises ApiError."""
    # seg: path segments after /api/v3, e.g. ["repos","adaiversen","amber-thistle","issues"]
    if not seg:
        return 200, {"X-OAuth-Scopes": "repo, read:org"}, {
            "current_user_url": "/api/v3/user",
            "repository_url": "/api/v3/repos/{owner}/{repo}",
            "user_repositories_url": "/api/v3/user/repos",
        }
    if seg == ["meta"]:
        if method != "GET":
            raise ApiError(404, "Not Found")
        return 200, {}, {"installed_version": VERSION}
    if seg == ["user"]:
        if method != "GET":
            raise ApiError(404, "Not Found")
        return 200, {}, user_view(VIEWER["login"], VIEWER["id"], VIEWER["name"])
    if seg == ["user", "repos"]:
        if method == "POST":
            name = body.get("name") if isinstance(body, dict) else None
            if not isinstance(name, str) or not name:
                raise ApiError(422, "name is required")
            with STORE.lock:
                if (VIEWER["login"], name) in STORE.repos:
                    raise ApiError(422, "Repository already exists")
                rid = STORE.next_repo_id
                STORE.next_repo_id += 1
                seq = STORE.next_seq
                STORE.next_seq += 1
                repo = Repo(VIEWER["login"], name, rid,
                            body.get("description") or "",
                            bool(body.get("private", False)),
                            body.get("default_branch") or "main", seq)
                STORE.repos[(VIEWER["login"], name)] = repo
            return 201, {}, repo_view(repo, host)
        if method == "GET":
            with STORE.lock:
                repos = [r for (o, _), r in STORE.repos.items()
                         if o == VIEWER["login"]]
            repos.sort(key=lambda r: r.seq)
            chunk, extra = _paginate([repo_view(r, host) for r in repos],
                                     query, base_url)
            return 200, extra, chunk
        raise ApiError(404, "Not Found")
    if len(seg) >= 3 and seg[0] == "repos":
        owner, name = seg[1], seg[2]
        repo = get_repo_or_none(owner, name)
        rest = seg[3:]
        if repo is None:
            raise ApiError(404, "Not Found")
        if not rest:
            if method == "GET":
                return 200, {}, repo_view(repo, host)
            if method == "PATCH":
                with STORE.lock:
                    if isinstance(body, dict):
                        if "description" in body:
                            repo.description = body["description"] or ""
                        if "private" in body:
                            repo.private = bool(body["private"])
                        if "default_branch" in body and body["default_branch"]:
                            repo.default_branch = body["default_branch"]
                return 200, {}, repo_view(repo, host)
            if method == "DELETE":
                with STORE.lock:
                    STORE.repos.pop((owner, name), None)
                return 204, {}, None
            raise ApiError(404, "Not Found")
        if rest == ["branches"]:
            if method != "GET":
                raise ApiError(404, "Not Found")
            out = [{"name": ref[len("refs/heads/"):],
                    "commit": {"sha": oid, "url": ""},
                    "protected": False}
                   for ref, oid in sorted(repo.refs.items())
                   if ref.startswith("refs/heads/")]
            return 200, {}, out
        if len(rest) == 2 and rest[0] == "branches":
            if method != "GET":
                raise ApiError(404, "Not Found")
            oid = _branch_tip(repo, rest[1])
            if oid is None:
                raise ApiError(404, "Branch not found")
            return 200, {}, {"name": rest[1], "commit": {"sha": oid, "url": ""},
                            "protected": False}
        if rest == ["commits"]:
            if method != "GET":
                raise ApiError(404, "Not Found")
            sha = query.get("sha", [None])[0]
            tip = None
            if sha:
                if HEX40.match(sha or ""):
                    tip = sha if sha in STORE.objects else None
                else:
                    tip = _branch_tip(repo, sha)
                    if tip is None and sha == "HEAD":
                        tip = _branch_tip(repo, repo.default_branch)
            else:
                tip = _branch_tip(repo, repo.default_branch)
            if tip is None:
                raise ApiError(404, "Not Found")
            out = []
            for oid in _commit_chain(STORE, tip):
                _, _, author, committer, message = parse_commit(STORE.objects[oid][1])
                out.append({"sha": oid,
                            "commit": {"message": message,
                                       "author": {"name": author, "email": "", "date": FIXED_TS},
                                       "committer": {"name": committer, "email": "", "date": FIXED_TS}},
                            "author": None, "committer": None,
                            "parents": []})
            return 200, {}, out
        if rest == ["git", "refs"]:
            if method != "POST":
                raise ApiError(404, "Not Found")
            ref = body.get("ref") if isinstance(body, dict) else None
            sha = body.get("sha") if isinstance(body, dict) else None
            if not isinstance(ref, str) or not ref.startswith("refs/"):
                raise ApiError(422, "ref is required")
            if not isinstance(sha, str) or sha not in STORE.objects:
                raise ApiError(422, "sha does not exist")
            with STORE.lock:
                if ref in repo.refs:
                    raise ApiError(422, "Reference already exists")
                repo.refs[ref] = sha
            return 201, {}, {"ref": ref, "object": {"sha": sha, "type": "commit",
                                                    "url": ""},
                            "url": f"/api/v3/repos/{owner}/{name}/git/refs/{ref}"}
        if len(rest) == 4 and rest[:3] == ["git", "refs", "heads"]:
            if method != "GET":
                raise ApiError(404, "Not Found")
            oid = _branch_tip(repo, rest[3])
            if oid is None:
                raise ApiError(404, "Not Found")
            ref = f"refs/heads/{rest[3]}"
            return 200, {}, {"ref": ref, "object": {"sha": oid, "type": "commit", "url": ""},
                            "url": f"/api/v3/repos/{owner}/{name}/git/refs/{ref}"}
        if rest and rest[0] == "contents":
            return _handle_contents(method, repo, rest[1:], query, body, host)
        if rest == ["labels"]:
            if method != "POST":
                raise ApiError(404, "Not Found")
            lname = body.get("name") if isinstance(body, dict) else None
            if not isinstance(lname, str) or not lname:
                raise ApiError(422, "name is required")
            color = body.get("color") if isinstance(body, dict) else None
            with STORE.lock:
                repo.labels[lname] = {"name": lname,
                                      "color": color or "ededed",
                                      "description": ""}
            return 201, {}, label_view(lname, repo.labels[lname]["color"])
        if rest == ["issues"]:
            if method == "POST":
                title = body.get("title") if isinstance(body, dict) else None
                if not isinstance(title, str) or not title:
                    raise ApiError(422, "title is required")
                raw_labels = body.get("labels") or []
                labels = []
                for item in raw_labels:
                    lname = item if isinstance(item, str) else item.get("name")
                    if lname not in repo.labels:
                        raise ApiError(422, f"label does not exist: {lname}")
                    labels.append(lname)
                issue = create_issue_in_repo(repo, title, body.get("body"), labels)
                return 201, {}, issue_view(repo, issue, host)
            if method == "GET":
                state = query.get("state", ["open"])[0]
                sort_dir = query.get("direction", ["desc"])[0]
                issues = list(repo.issues.values())
                if state != "all":
                    issues = [i for i in issues if i.state == state]
                issues.sort(key=lambda i: i.seq, reverse=(sort_dir != "asc"))
                chunk, extra = _paginate([issue_view(repo, i, host) for i in issues],
                                         query, base_url)
                return 200, extra, chunk
            raise ApiError(404, "Not Found")
        if len(rest) == 2 and rest[0] == "issues" and rest[1].isdigit():
            num = int(rest[1])
            issue = repo.issues.get(num)
            if issue is None:
                raise ApiError(404, "Not Found")
            if method == "GET":
                return 200, {}, issue_view(repo, issue, host)
            if method == "PATCH":
                with STORE.lock:
                    if isinstance(body, dict):
                        if "state" in body and body["state"] in ("open", "closed"):
                            issue.state = body["state"]
                            issue.closed_at = FIXED_TS if body["state"] == "closed" else None
                        if "title" in body and body["title"]:
                            issue.title = body["title"]
                        if "body" in body:
                            issue.body = body["body"] or ""
                return 200, {}, issue_view(repo, issue, host)
            raise ApiError(404, "Not Found")
        if len(rest) == 3 and rest[0] == "issues" and rest[1].isdigit() and rest[2] == "comments":
            num = int(rest[1])
            issue = repo.issues.get(num)
            if issue is None:
                raise ApiError(404, "Not Found")
            if method == "POST":
                cbody = body.get("body") if isinstance(body, dict) else None
                if not isinstance(cbody, str):
                    raise ApiError(422, "body is required")
                with STORE.lock:
                    cid = STORE.next_comment_gid
                    STORE.next_comment_gid += 1
                    seq = STORE.next_seq
                    STORE.next_seq += 1
                    comment = Comment(cid, num, cbody, seq)
                    repo.comments[cid] = comment
                    issue.comments.append(cid)
                return 201, {}, comment_view(repo, comment, host)
            if method == "GET":
                comments = [repo.comments[cid] for cid in issue.comments
                            if cid in repo.comments]
                comments.sort(key=lambda c: c.seq)
                return 200, {}, [comment_view(repo, c, host) for c in comments]
            raise ApiError(404, "Not Found")
        if rest == ["pulls"]:
            if method == "POST":
                head = body.get("head") if isinstance(body, dict) else None
                base = body.get("base") if isinstance(body, dict) else None
                title = body.get("title") if isinstance(body, dict) else None
                if not head or not base or not title:
                    raise ApiError(422, "head, base and title are required")
                head_tip = _branch_tip(repo, head)
                base_tip = _branch_tip(repo, base)
                if head_tip is None or base_tip is None:
                    raise ApiError(422, "head or base branch does not exist")
                with STORE.lock:
                    for pr in repo.pulls.values():
                        if pr.state == "open" and pr.head == head and pr.base == base:
                            raise ApiError(422, "a pull request already exists for these branches")
                    mb = merge_base(STORE, head_tip, base_tip)
                    pr = create_pull_in_repo(repo, title, body.get("body"),
                                             head, base, head_tip, base_tip, mb)
                return 201, {}, pull_view(repo, pr, host)
            if method == "GET":
                state = query.get("state", ["open"])[0]
                pulls = list(repo.pulls.values())
                if state != "all":
                    pulls = [p for p in pulls if p.state == state]
                pulls.sort(key=lambda p: p.seq, reverse=True)
                chunk, extra = _paginate([pull_view(repo, p, host) for p in pulls],
                                         query, base_url)
                return 200, extra, chunk
            raise ApiError(404, "Not Found")
        if len(rest) == 2 and rest[0] == "pulls" and rest[1].isdigit():
            num = int(rest[1])
            pr = repo.pulls.get(num)
            if pr is None:
                raise ApiError(404, "Not Found")
            if method == "GET":
                return 200, {}, pull_view(repo, pr, host)
            raise ApiError(404, "Not Found")
        if len(rest) == 3 and rest[0] == "pulls" and rest[1].isdigit() and rest[2] == "files":
            if method != "GET":
                raise ApiError(404, "Not Found")
            num = int(rest[1])
            pr = repo.pulls.get(num)
            if pr is None:
                raise ApiError(404, "Not Found")
            base_files = STORE.files_of_commit(pr.merge_base) if pr.merge_base else {}
            head_files = STORE.files_of_commit(pr.head_sha)
            return 200, {}, diff_files(base_files, head_files)
        if len(rest) == 3 and rest[0] == "pulls" and rest[1].isdigit() and rest[2] == "merge":
            if method != "PUT":
                raise ApiError(404, "Not Found")
            num = int(rest[1])
            pr = repo.pulls.get(num)
            if pr is None:
                raise ApiError(404, "Not Found")
            if pr.state != "open":
                raise ApiError(405, "Pull request is already merged or closed")
            with STORE.lock:
                if pr_mergeable(STORE, repo, pr) == "CONFLICTING":
                    raise ApiError(405, "Merge conflict")
                ok, result = do_merge(STORE, repo, pr)
            if not ok:
                raise ApiError(405, str(result))
            return 200, {}, {"sha": result, "merged": True,
                            "message": f"{pr.title} (#{pr.number})"}
        raise ApiError(404, "Not Found")
    raise ApiError(404, "Not Found")


def _handle_contents(method: str, repo: Repo, path_seg, query, body, host: str):
    path = "/".join(path_seg)
    ref = query.get("ref", [None])[0]
    if ref:
        tip = _branch_tip(repo, ref)
        if tip is None and HEX40.match(ref or ""):
            tip = ref if ref in STORE.objects else None
    else:
        tip = _branch_tip(repo, repo.default_branch)
    if method == "GET":
        entry = _contents_entry(repo, tip, path, host)
        if entry is None:
            raise ApiError(404, "Not Found")
        return 200, {}, entry
    if method == "PUT":
        if not isinstance(body, dict):
            raise ApiError(400, "Invalid request body")
        message = body.get("message")
        content_b64 = body.get("content")
        if not isinstance(message, str) or not isinstance(content_b64, str):
            raise ApiError(422, "message and content are required")
        try:
            data = base64.b64decode(content_b64, validate=False)
        except Exception:
            raise ApiError(422, "content is not valid base64")
        branch = body.get("branch") or repo.default_branch
        given_sha = body.get("sha")
        with STORE.lock:
            branch_tip = _branch_tip(repo, branch)
            files = STORE.files_of_commit(branch_tip) if branch_tip else {}
            existed = path in files
            if existed:
                if given_sha is None:
                    raise ApiError(409, "sha is required to update a file")
                if given_sha != files[path][1]:
                    raise ApiError(409, "sha does not match the current file")
            blob_oid = STORE.put_object("blob", data)
            files = dict(files)
            files[path] = ("100644", blob_oid)
            tree_oid = STORE.build_tree_from_files(
                {p: (m, STORE.objects[o][1]) for p, (m, o) in files.items()})
            parents = [branch_tip] if branch_tip else []
            commit_oid = make_commit(STORE, tree_oid, parents, message)
            repo.refs[f"refs/heads/{branch}"] = commit_oid
        entry = {"name": path.rsplit("/", 1)[-1], "path": path, "sha": blob_oid,
                 "size": len(data), "encoding": "base64",
                 "content": content_b64,
                 "url": f"/api/v3/repos/{repo.owner}/{repo.name}/contents/{path}"}
        return (201 if not existed else 200), {}, {
            "content": entry,
            "commit": {"sha": commit_oid, "message": message}}
    if method == "DELETE":
        if not isinstance(body, dict):
            raise ApiError(400, "Invalid request body")
        message = body.get("message")
        given_sha = body.get("sha")
        if not isinstance(message, str):
            raise ApiError(422, "message is required")
        branch = body.get("branch") or repo.default_branch
        with STORE.lock:
            branch_tip = _branch_tip(repo, branch)
            files = STORE.files_of_commit(branch_tip) if branch_tip else {}
            if path not in files:
                raise ApiError(404, "Not Found")
            if given_sha is not None and given_sha != files[path][1]:
                raise ApiError(409, "sha does not match the current file")
            files = dict(files)
            del files[path]
            tree_oid = STORE.build_tree_from_files(
                {p: (m, STORE.objects[o][1]) for p, (m, o) in files.items()})
            parents = [branch_tip] if branch_tip else []
            commit_oid = make_commit(STORE, tree_oid, parents, message)
            repo.refs[f"refs/heads/{branch}"] = commit_oid
        return 200, {}, {"commit": {"sha": commit_oid, "message": message}}
    raise ApiError(404, "Not Found")

# ---------------------------------------------------------------------------
# GraphQL
# ---------------------------------------------------------------------------

_GQL_FIELDS = {
    "Issue": ["id", "number", "title", "state", "body", "createdAt", "updatedAt"],
    "PullRequest": ["id", "number", "title", "body", "state", "merged",
                    "headRefName", "baseRefName", "mergeable", "files",
                    "createdAt", "updatedAt"],
    "Repository": ["id", "databaseId", "name", "nameWithOwner", "owner",
                   "defaultBranchRef", "viewerPermission", "hasIssuesEnabled",
                   "issue", "issues", "pullRequests", "pullRequest",
                   "createdAt", "updatedAt", "description", "url"],
    "User": ["id", "login", "name", "email", "avatarUrl"],
    "IssueConnection": ["nodes", "edges", "pageInfo", "totalCount"],
    "PullRequestConnection": ["nodes", "edges", "pageInfo", "totalCount"],
    "IssueComment": ["id", "body", "createdAt", "updatedAt"],
    "SearchType": [],
}
_GQL_ENUMS = {
    "SearchType": ["ISSUE", "REPOSITORY", "USER", "COMMIT", "DISCUSSION"],
    "IssueState": ["OPEN", "CLOSED"],
    "PullRequestState": ["OPEN", "CLOSED", "MERGED"],
    "MergeableState": ["MERGEABLE", "CONFLICTING", "UNKNOWN"],
}


class _GqlError(Exception):
    pass


def _gql_tokenize(text: str):
    tokens = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r\n,":
            i += 1
        elif ch == "#":
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("...", i):
            tokens.append(("...", "..."))
            i += 3
        elif ch in "{}():$![]=@":
            tokens.append((ch, ch))
            i += 1
        elif ch == '"':
            j = i + 1
            out = []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    esc = text[j + 1]
                    out.append({"n": "\n", "t": "\t", "r": "\r", '"': '"',
                                "\\": "\\", "/": "/"}.get(esc, esc))
                    j += 2
                else:
                    out.append(text[j])
                    j += 1
            tokens.append(("string", "".join(out)))
            i = j + 1
        elif ch.isdigit() or (ch == "-" and i + 1 < n and text[i + 1].isdigit()):
            j = i
            while j < n and (text[j].isdigit() or text[j] in ".eE+-"):
                j += 1
            raw = text[i:j]
            try:
                tokens.append(("int", int(raw)))
            except ValueError:
                tokens.append(("float", float(raw)))
            i = j
        elif ch.isalpha() or ch == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            tokens.append(("name", text[i:j]))
            i = j
        else:
            raise _GqlError(f"unexpected character {ch!r}")
    tokens.append(("eof", None))
    return tokens


class _GqlParser:
    def __init__(self, text: str):
        self.tokens = _gql_tokenize(text)
        self.pos = 0
        self.fragments = {}

    def peek(self):
        return self.tokens[self.pos]

    def next(self):
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def expect(self, kind, value=None):
        tok = self.next()
        if tok[0] != kind or (value is not None and tok[1] != value):
            raise _GqlError(f"expected {kind} {value!r}, got {tok}")
        return tok

    def parse(self):
        operations = []
        while self.peek()[0] != "eof":
            if self.peek() == ("name", "fragment"):
                self.next()
                name = self.expect("name")[1]
                self.expect("name", "on")
                type_cond = self.expect("name")[1]
                sels = self.parse_selection_set()
                self.fragments[name] = (type_cond, sels)
                continue
            kind = "query"
            name = None
            if self.peek()[0] == "name" and self.peek()[1] in ("query", "mutation", "subscription"):
                kind = self.next()[1]
                if self.peek()[0] == "name":
                    name = self.next()[1]
            vardefs = {}
            if self.peek() == ("(", "("):
                self.next()
                while self.peek() != (")", ")"):
                    self.expect("$")
                    vname = self.expect("name")[1]
                    self.expect(":")
                    self._skip_type()
                    default = None
                    if self.peek() == ("=", "="):
                        self.next()
                        default = self.parse_value({})
                    vardefs[vname] = default
                self.expect(")")
            sels = self.parse_selection_set()
            operations.append({"kind": kind, "name": name, "vardefs": vardefs,
                               "selections": sels})
        return operations

    def _skip_type(self):
        tok = self.next()
        if tok == ("[", "["):
            self._skip_type()
            self.expect("]")
        elif tok[0] != "name":
            raise _GqlError(f"bad type {tok}")
        while self.peek() == ("!", "!"):
            self.next()

    def parse_selection_set(self):
        self.expect("{")
        sels = []
        while self.peek() != ("}", "}"):
            if self.peek() == ("...", "..."):
                self.next()
                if self.peek() == ("name", "on"):
                    self.next()
                    type_cond = self.expect("name")[1]
                    sels.append({"fragment": True, "type_condition": type_cond,
                                 "selections": self.parse_selection_set()})
                else:
                    frag_name = self.expect("name")[1]
                    sels.append({"fragment": True, "spread": frag_name,
                                 "selections": []})
                continue
            first = self.expect("name")[1]
            alias = None
            field = first
            if self.peek() == (":", ":"):
                self.next()
                alias = first
                field = self.expect("name")[1]
            args = {}
            if self.peek() == ("(", "("):
                self.next()
                while self.peek() != (")", ")"):
                    aname = self.expect("name")[1]
                    self.expect(":")
                    args[aname] = self.parse_value({})
                self.expect(")")
            sub = []
            if self.peek() == ("{", "{"):
                sub = self.parse_selection_set()
            sels.append({"field": field, "alias": alias, "args": args,
                         "selections": sub})
        self.expect("}")
        return sels

    def parse_value(self, variables):
        tok = self.next()
        if tok[0] == "$":
            return {"$var": self.expect("name")[1]}
        if tok[0] in ("string", "int", "float"):
            return tok[1]
        if tok == ("[", "["):
            items = []
            while self.peek() != ("]", "]"):
                items.append(self.parse_value(variables))
            self.expect("]")
            return items
        if tok == ("{", "{"):
            obj = {}
            while self.peek() != ("}", "}"):
                key = self.expect("name")[1]
                self.expect(":")
                obj[key] = self.parse_value(variables)
            self.expect("}")
            return obj
        if tok[0] == "name":
            if tok[1] == "true":
                return True
            if tok[1] == "false":
                return False
            if tok[1] == "null":
                return None
            return tok[1]  # enum value, as a string
        raise _GqlError(f"bad value {tok}")


def _resolve_value(value, variables):
    if isinstance(value, dict):
        if set(value.keys()) == {"$var"}:
            return variables.get(value["$var"])
        return {k: _resolve_value(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_value(v, variables) for v in value]
    return value


def _cursor_encode(seq: int) -> str:
    return base64.urlsafe_b64encode(str(seq).encode("ascii")).decode("ascii")


def _cursor_decode(cursor):
    try:
        return int(base64.urlsafe_b64decode(str(cursor).encode("ascii")).decode("ascii"))
    except Exception:
        return None


def _repo_by_dbid(dbid):
    for repo in STORE.repos.values():
        if repo.id == dbid:
            return repo
    return None


def _find_by_gid(gid_value):
    decoded = gid_decode(gid_value)
    if decoded is None:
        return None
    kind, parts = decoded
    if kind == "Repository" and len(parts) == 1:
        try:
            return ("Repository", _repo_by_dbid(int(parts[0])))
        except ValueError:
            return None
    if kind in ("Issue", "PullRequest") and len(parts) == 3:
        owner, name, num = parts[0], parts[1], parts[2]
        repo = STORE.repos.get((owner, name))
        if repo is None:
            return None
        try:
            n = int(num)
        except ValueError:
            return None
        if kind == "Issue":
            return ("Issue", (repo, repo.issues.get(n)))
        return ("PullRequest", (repo, repo.pulls.get(n)))
    return None


def _wrap_repo(repo):
    return {"__typename": "Repository", "repo": repo}


def _wrap_issue(repo, issue):
    return {"__typename": "Issue", "repo": repo, "issue": issue}


def _wrap_pr(repo, pr):
    return {"__typename": "PullRequest", "repo": repo, "pr": pr}


def _wrap_user(login, uid, name):
    return {"__typename": "User", "login": login, "id": uid, "name": name,
            "email": f"{login}@example.invalid"}


def _issue_state_gql(state: str) -> str:
    return "OPEN" if state == "open" else "CLOSED"


def _pr_state_gql(pr: Pull) -> str:
    if pr.merged:
        return "MERGED"
    return "OPEN" if pr.state == "open" else "CLOSED"


def _gql_field(obj, field, args, variables):
    """Resolve one field on a wrapped object. Raises _GqlError for unknown fields."""
    tname = obj.get("__typename")
    if field == "__typename":
        return tname
    if tname == "Query":
        if field == "viewer":
            return _wrap_user(VIEWER["login"], VIEWER["id"], VIEWER["name"])
        if field == "repository":
            owner = args.get("owner")
            name = args.get("name")
            repo = get_repo_or_none(owner, name) if owner and name else None
            return _wrap_repo(repo) if repo else None
        if field == "node":
            found = _find_by_gid(args.get("id"))
            if found is None:
                return None
            kind, payload = found
            if kind == "Repository":
                return _wrap_repo(payload) if payload else None
            if kind == "Issue":
                repo, issue = payload
                return _wrap_issue(repo, issue) if issue else None
            repo, pr = payload
            return _wrap_pr(repo, pr) if pr else None
        if field == "__type":
            name = args.get("name")
            if name in _GQL_FIELDS:
                return {"__typename": "__Type", "name": name,
                        "kind": "ENUM" if name in _GQL_ENUMS else "OBJECT"}
            if name in _GQL_ENUMS:
                return {"__typename": "__Type", "name": name, "kind": "ENUM"}
            return None
        raise _GqlError(f'Cannot query field "{field}" on type "Query".')
    if tname == "Mutation":
        return _gql_mutation(field, args, variables)
    if tname == "__Type":
        if field == "name":
            return obj["name"]
        if field == "kind":
            return obj["kind"]
        if field == "fields":
            return [{"name": f} for f in _GQL_FIELDS.get(obj["name"], [])]
        if field == "enumValues":
            return [{"name": v} for v in _GQL_ENUMS.get(obj["name"], [])]
        raise _GqlError(f'Cannot query field "{field}" on type "__Type".')
    if tname == "User":
        if field in ("login", "name", "email"):
            return obj[field]
        if field == "id":
            return gid_encode("User", obj["login"])
        if field == "avatarUrl":
            return ""
        raise _GqlError(f'Cannot query field "{field}" on type "User".')
    if tname == "Repository":
        repo = obj["repo"]
        if field == "id":
            return gid_encode("Repository", str(repo.id))
        if field == "databaseId":
            return repo.id
        if field == "name":
            return repo.name
        if field == "nameWithOwner":
            return f"{repo.owner}/{repo.name}"
        if field == "owner":
            return _wrap_user(repo.owner, VIEWER["id"], VIEWER["name"])
        if field == "defaultBranchRef":
            tip = _branch_tip(repo, repo.default_branch)
            if tip is None:
                return None
            return {"__typename": "Ref", "name": repo.default_branch}
        if field == "viewerPermission":
            return "ADMIN"
        if field == "hasIssuesEnabled":
            return True
        if field == "description":
            return repo.description
        if field == "url":
            return f"/api/v3/repos/{repo.owner}/{repo.name}"
        if field == "createdAt" or field == "updatedAt":
            return FIXED_TS
        if field == "issue":
            num = args.get("number")
            issue = repo.issues.get(num) if isinstance(num, int) else None
            return _wrap_issue(repo, issue) if issue else None
        if field == "issues":
            return _issues_connection(repo, args)
        if field == "pullRequests":
            return _prs_connection(repo, args)
        if field == "pullRequest":
            num = args.get("number")
            pr = repo.pulls.get(num) if isinstance(num, int) else None
            return _wrap_pr(repo, pr) if pr else None
        raise _GqlError(f'Cannot query field "{field}" on type "Repository".')
    if tname == "Ref":
        if field == "name":
            return obj["name"]
        raise _GqlError(f'Cannot query field "{field}" on type "Ref".')
    if tname == "Issue":
        issue = obj["issue"]
        repo = obj["repo"]
        if field == "id":
            return gid_encode("Issue", repo.owner, repo.name, str(issue.number))
        if field == "number":
            return issue.number
        if field == "title":
            return issue.title
        if field == "state":
            return _issue_state_gql(issue.state)
        if field == "body":
            return issue.body
        if field in ("createdAt", "updatedAt"):
            return FIXED_TS
        raise _GqlError(f'Cannot query field "{field}" on type "Issue".')
    if tname == "PullRequest":
        pr = obj["pr"]
        repo = obj["repo"]
        if field == "id":
            return gid_encode("PullRequest", repo.owner, repo.name, str(pr.number))
        if field == "number":
            return pr.number
        if field == "title":
            return pr.title
        if field == "body":
            return pr.body
        if field == "state":
            return _pr_state_gql(pr)
        if field == "merged":
            return pr.merged
        if field == "headRefName":
            return pr.head
        if field == "baseRefName":
            return pr.base
        if field == "mergeable":
            return pr_mergeable(STORE, repo, pr)
        if field == "files":
            first = args.get("first")
            base_files = STORE.files_of_commit(pr.merge_base) if pr.merge_base else {}
            head_files = STORE.files_of_commit(pr.head_sha)
            nodes = [{"__typename": "PullRequestFile", "path": f["filename"]}
                     for f in diff_files(base_files, head_files)]
            if isinstance(first, int):
                nodes = nodes[:first]
            return {"__typename": "PullRequestFileConnection", "nodes": nodes}
        if field in ("createdAt", "updatedAt"):
            return FIXED_TS
        raise _GqlError(f'Cannot query field "{field}" on type "PullRequest".')
    if tname == "PullRequestFile":
        if field == "path":
            return obj["path"]
        raise _GqlError(f'Cannot query field "{field}" on type "PullRequestFile".')
    if tname in ("IssueConnection", "PullRequestConnection",
                 "PullRequestFileConnection"):
        if field == "nodes":
            return obj["nodes"]
        if field == "edges":
            return [{"node": n, "cursor": _cursor_encode(n.get("_seq", 0))}
                    for n in obj["nodes"]]
        if field == "pageInfo":
            return {"__typename": "PageInfo",
                    "hasNextPage": obj.get("has_next", False),
                    "endCursor": obj.get("end_cursor")}
        if field == "totalCount":
            return obj.get("total", len(obj["nodes"]))
        raise _GqlError(f'Cannot query field "{field}" on type "{tname}".')
    if tname == "PageInfo":
        if field == "hasNextPage":
            return obj["hasNextPage"]
        if field == "endCursor":
            return obj["endCursor"]
        raise _GqlError(f'Cannot query field "{field}" on type "PageInfo".')
    if tname == "IssueComment":
        comment = obj["comment"]
        repo = obj["repo"]
        if field == "id":
            return gid_encode("IssueComment", repo.owner, repo.name,
                              str(comment.issue_number), str(comment.id))
        if field == "body":
            return comment.body
        if field in ("createdAt", "updatedAt"):
            return FIXED_TS
        raise _GqlError(f'Cannot query field "{field}" on type "IssueComment".')
    if tname in ("CreateIssuePayload", "CloseIssuePayload"):
        if field == "issue":
            return obj["issue"]
        raise _GqlError(f'Cannot query field "{field}" on type "{tname}".')
    if tname == "CreatePullRequestPayload":
        if field == "pullRequest":
            return obj["pullRequest"]
        raise _GqlError(f'Cannot query field "{field}" on type "{tname}".')
    if tname == "MergePullRequestPayload":
        if field == "pullRequest":
            return obj["pullRequest"]
        raise _GqlError(f'Cannot query field "{field}" on type "{tname}".')
    if tname == "AddCommentPayload":
        if field == "commentEdge":
            return obj["commentEdge"]
        raise _GqlError(f'Cannot query field "{field}" on type "{tname}".')
    if tname == "IssueCommentEdge":
        if field == "node":
            return obj["node"]
        raise _GqlError(f'Cannot query field "{field}" on type "IssueCommentEdge".')
    raise _GqlError(f'Cannot query field "{field}" on type "{tname}".')


def _issues_connection(repo: Repo, args):
    issues = list(repo.issues.values())
    states = args.get("states")
    if states:
        wanted = {s.lower() for s in states if isinstance(s, str)}
        issues = [i for i in issues if i.state in wanted]
    order_by = args.get("orderBy") or {}
    direction = (order_by.get("direction") or "DESC").upper()
    issues.sort(key=lambda i: i.seq, reverse=(direction != "ASC"))
    after = args.get("after")
    if after:
        seq = _cursor_decode(after)
        if seq is not None:
            issues = [i for i in issues
                      if (i.seq < seq if direction != "ASC" else i.seq > seq)]
    total = len(issues)
    first = args.get("first")
    page = issues[:first] if isinstance(first, int) else issues
    has_next = len(issues) > len(page)
    end_cursor = _cursor_encode(page[-1].seq) if page else None
    nodes = []
    for issue in page:
        wrapped = _wrap_issue(repo, issue)
        wrapped["_seq"] = issue.seq
        nodes.append(wrapped)
    return {"__typename": "IssueConnection", "nodes": nodes,
            "has_next": has_next, "end_cursor": end_cursor, "total": total}


def _prs_connection(repo: Repo, args):
    pulls = sorted(repo.pulls.values(), key=lambda p: p.seq, reverse=True)
    total = len(pulls)
    first = args.get("first")
    page = pulls[:first] if isinstance(first, int) else pulls
    has_next = len(pulls) > len(page)
    end_cursor = _cursor_encode(page[-1].seq) if page else None
    nodes = []
    for pr in page:
        wrapped = _wrap_pr(repo, pr)
        wrapped["_seq"] = pr.seq
        nodes.append(wrapped)
    return {"__typename": "PullRequestConnection", "nodes": nodes,
            "has_next": has_next, "end_cursor": end_cursor, "total": total}


def _gql_mutation(field, args, variables):
    inp = args.get("input") or {}
    if not isinstance(inp, dict):
        raise _GqlError(f'mutation {field} needs an input object')
    if field == "createIssue":
        found = _find_by_gid(inp.get("repositoryId"))
        if found is None or found[0] != "Repository" or found[1] is None:
            raise _GqlError("repository not found")
        repo = found[1]
        title = inp.get("title")
        if not title:
            raise _GqlError("title is required")
        issue = create_issue_in_repo(repo, title, inp.get("body"), [])
        return {"__typename": "CreateIssuePayload",
                "issue": _wrap_issue(repo, issue)}
    if field == "closeIssue":
        found = _find_by_gid(inp.get("issueId"))
        if found is None or found[0] != "Issue":
            raise _GqlError("issue not found")
        repo, issue = found[1]
        if issue is None:
            raise _GqlError("issue not found")
        with STORE.lock:
            issue.state = "closed"
            issue.closed_at = FIXED_TS
        return {"__typename": "CloseIssuePayload",
                "issue": _wrap_issue(repo, issue)}
    if field == "createPullRequest":
        found = _find_by_gid(inp.get("repositoryId"))
        if found is None or found[0] != "Repository" or found[1] is None:
            raise _GqlError("repository not found")
        repo = found[1]
        head = inp.get("headRefName")
        base = inp.get("baseRefName")
        title = inp.get("title")
        if not head or not base or not title:
            raise _GqlError("headRefName, baseRefName and title are required")
        head_tip = _branch_tip(repo, head)
        base_tip = _branch_tip(repo, base)
        if head_tip is None or base_tip is None:
            raise _GqlError("head or base branch does not exist")
        with STORE.lock:
            for pr in repo.pulls.values():
                if pr.state == "open" and pr.head == head and pr.base == base:
                    raise _GqlError("a pull request already exists for these branches")
            mb = merge_base(STORE, head_tip, base_tip)
            pr = create_pull_in_repo(repo, title, "", head, base,
                                     head_tip, base_tip, mb)
        return {"__typename": "CreatePullRequestPayload",
                "pullRequest": _wrap_pr(repo, pr)}
    if field == "mergePullRequest":
        found = _find_by_gid(inp.get("pullRequestId"))
        if found is None or found[0] != "PullRequest":
            raise _GqlError("pull request not found")
        repo, pr = found[1]
        if pr is None:
            raise _GqlError("pull request not found")
        if pr.state != "open":
            raise _GqlError("pull request is already merged or closed")
        with STORE.lock:
            if pr_mergeable(STORE, repo, pr) == "CONFLICTING":
                raise _GqlError("merge conflict")
            ok, result = do_merge(STORE, repo, pr)
        if not ok:
            raise _GqlError(str(result))
        return {"__typename": "MergePullRequestPayload",
                "pullRequest": _wrap_pr(repo, pr)}
    if field == "addComment":
        found = _find_by_gid(inp.get("subjectId"))
        if found is None or found[0] != "Issue":
            raise _GqlError("subject not found")
        repo, issue = found[1]
        if issue is None:
            raise _GqlError("subject not found")
        body = inp.get("body")
        if not isinstance(body, str):
            raise _GqlError("body is required")
        with STORE.lock:
            cid = STORE.next_comment_gid
            STORE.next_comment_gid += 1
            seq = STORE.next_seq
            STORE.next_seq += 1
            comment = Comment(cid, issue.number, body, seq)
            repo.comments[cid] = comment
            issue.comments.append(cid)
        node = {"__typename": "IssueComment", "repo": repo, "comment": comment}
        return {"__typename": "AddCommentPayload",
                "commentEdge": {"__typename": "IssueCommentEdge", "node": node}}
    raise _GqlError(f'Cannot query field "{field}" on type "Mutation".')


def _execute_selections(obj, selections, variables, errors, fragments):
    if obj is None:
        return None
    if isinstance(obj, list):
        return [_execute_selections(item, selections, variables, errors, fragments)
                for item in obj]
    if not isinstance(obj, dict) or "__typename" not in obj:
        return obj
    out = {}
    for sel in selections:
        if sel.get("fragment"):
            if "spread" in sel:
                type_cond, frag_sels = fragments.get(sel["spread"], (None, []))
                if type_cond is None or type_cond == obj["__typename"]:
                    out.update(_execute_selections(obj, frag_sels, variables, errors, fragments) or {})
                continue
            if sel.get("type_condition") in (None, obj["__typename"]):
                out.update(_execute_selections(obj, sel["selections"], variables, errors, fragments) or {})
            continue
        field = sel["field"]
        key = sel["alias"] or field
        args = {k: _resolve_value(v, variables) for k, v in sel["args"].items()}
        try:
            value = _gql_field(obj, field, args, variables)
        except _GqlError as exc:
            errors.append({"message": str(exc)})
            out[key] = None
            continue
        if sel["selections"] and value is not None:
            value = _execute_selections(value, sel["selections"], variables, errors, fragments)
        out[key] = value
    return out


def execute_graphql(payload):
    """One operation body -> response dict."""
    query = payload.get("query")
    variables = payload.get("variables") or {}
    operation_name = payload.get("operationName")
    if not isinstance(query, str) or not query.strip():
        return {"data": None,
                "errors": [{"message": "query is required"}]}
    try:
        parser = _GqlParser(query)
        operations = parser.parse()
    except _GqlError as exc:
        return {"data": None, "errors": [{"message": str(exc)}]}
    if not operations:
        return {"data": None, "errors": [{"message": "no operation found"}]}
    op = None
    if operation_name:
        for candidate in operations:
            if candidate["name"] == operation_name:
                op = candidate
                break
    if op is None:
        op = operations[0]
    variables = dict(op["vardefs"])
    variables.update(payload.get("variables") or {})
    root_type = "Mutation" if op["kind"] == "mutation" else "Query"
    root = {"__typename": root_type}
    errors = []
    data = _execute_selections(root, op["selections"], variables, errors,
                               parser.fragments)
    response = {"data": data}
    if errors:
        response["errors"] = errors
    return response


def handle_graphql(body):
    """body: parsed JSON (dict or list) -> response object."""
    if isinstance(body, list):
        return [execute_graphql(item if isinstance(item, dict) else {})
                for item in body]
    if isinstance(body, dict):
        return execute_graphql(body)
    return {"data": None, "errors": [{"message": "invalid request body"}]}

# ---------------------------------------------------------------------------
# MCP (JSON-RPC 2.0 Streamable HTTP, revision 2026-07-28)
# ---------------------------------------------------------------------------

MCP_TOOLS = [
    {"name": "create_repo",
     "description": "Create a new repository owned by the authenticated user.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "name": {"type": "string", "description": "Repository name"},
                         "description": {"type": "string", "description": "Repository description"},
                         "private": {"type": "boolean", "description": "Whether the repository is private"}},
                     "required": ["name"]}},
    {"name": "get_repo",
     "description": "Get a repository by owner and name.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "owner": {"type": "string", "description": "Repository owner login"},
                         "repo": {"type": "string", "description": "Repository name"}},
                     "required": ["owner", "repo"]}},
    {"name": "create_issue",
     "description": "Create an issue in a repository.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "owner": {"type": "string", "description": "Repository owner login"},
                         "repo": {"type": "string", "description": "Repository name"},
                         "title": {"type": "string", "description": "Issue title"},
                         "body": {"type": "string", "description": "Issue body"}},
                     "required": ["owner", "repo", "title"]}},
    {"name": "list_issues",
     "description": "List issues in a repository.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "owner": {"type": "string", "description": "Repository owner login"},
                         "repo": {"type": "string", "description": "Repository name"},
                         "state": {"type": "string", "description": "Issue state: open, closed or all"}},
                     "required": ["owner", "repo"]}},
    {"name": "create_pull_request",
     "description": "Create a pull request in a repository.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "owner": {"type": "string", "description": "Repository owner login"},
                         "repo": {"type": "string", "description": "Repository name"},
                         "title": {"type": "string", "description": "Pull request title"},
                         "head": {"type": "string", "description": "Head branch name"},
                         "base": {"type": "string", "description": "Base branch name"},
                         "body": {"type": "string", "description": "Pull request body"}},
                     "required": ["owner", "repo", "title", "head", "base"]}},
    {"name": "merge_pull_request",
     "description": "Merge a pull request with a squash merge.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "owner": {"type": "string", "description": "Repository owner login"},
                         "repo": {"type": "string", "description": "Repository name"},
                         "pull_number": {"type": "integer", "description": "Pull request number"}},
                     "required": ["owner", "repo", "pull_number"]}},
]
_MCP_TOOL_NAMES = {t["name"] for t in MCP_TOOLS}
_MCP_SERVER_INFO = {"name": SERVER_NAME, "version": VERSION}


def _mcp_tool_call(name: str, args: dict, host: str):
    """-> (obj, is_error). Raises KeyError for missing required args."""
    def need(*keys):
        for key in keys:
            if key not in args or args[key] is None:
                raise KeyError(f"missing required argument: {key}")

    if name == "create_repo":
        need("name")
        repo_name = args["name"]
        with STORE.lock:
            if (VIEWER["login"], repo_name) in STORE.repos:
                return {"error": "Repository already exists"}, True
            rid = STORE.next_repo_id
            STORE.next_repo_id += 1
            seq = STORE.next_seq
            STORE.next_seq += 1
            repo = Repo(VIEWER["login"], repo_name, rid,
                        args.get("description") or "",
                        bool(args.get("private", False)), "main", seq)
            STORE.repos[(VIEWER["login"], repo_name)] = repo
        return repo_view(repo, host), False
    if name == "get_repo":
        need("owner", "repo")
        repo = get_repo_or_none(args["owner"], args["repo"])
        if repo is None:
            return {"error": "Not Found"}, True
        return repo_view(repo, host), False
    if name == "create_issue":
        need("owner", "repo", "title")
        repo = get_repo_or_none(args["owner"], args["repo"])
        if repo is None:
            return {"error": "Not Found"}, True
        issue = create_issue_in_repo(repo, args["title"], args.get("body"), [])
        return issue_view(repo, issue, host), False
    if name == "list_issues":
        need("owner", "repo")
        repo = get_repo_or_none(args["owner"], args["repo"])
        if repo is None:
            return {"error": "Not Found"}, True
        state = args.get("state") or "open"
        issues = list(repo.issues.values())
        if state != "all":
            issues = [i for i in issues if i.state == state]
        issues.sort(key=lambda i: i.seq, reverse=True)
        return {"issues": [issue_view(repo, i, host) for i in issues]}, False
    if name == "create_pull_request":
        need("owner", "repo", "title", "head", "base")
        repo = get_repo_or_none(args["owner"], args["repo"])
        if repo is None:
            return {"error": "Not Found"}, True
        head_tip = _branch_tip(repo, args["head"])
        base_tip = _branch_tip(repo, args["base"])
        if head_tip is None or base_tip is None:
            return {"error": "head or base branch does not exist"}, True
        with STORE.lock:
            for pr in repo.pulls.values():
                if pr.state == "open" and pr.head == args["head"] and pr.base == args["base"]:
                    return {"error": "a pull request already exists for these branches"}, True
            mb = merge_base(STORE, head_tip, base_tip)
            pr = create_pull_in_repo(repo, args["title"], args.get("body"),
                                     args["head"], args["base"],
                                     head_tip, base_tip, mb)
        return pull_view(repo, pr, host), False
    if name == "merge_pull_request":
        need("owner", "repo", "pull_number")
        repo = get_repo_or_none(args["owner"], args["repo"])
        if repo is None:
            return {"error": "Not Found"}, True
        pr = repo.pulls.get(args["pull_number"])
        if pr is None:
            return {"error": "Not Found"}, True
        if pr.state != "open":
            return {"error": "Pull request is already merged or closed"}, True
        with STORE.lock:
            if pr_mergeable(STORE, repo, pr) == "CONFLICTING":
                return {"error": "merge conflict"}, True
            ok, result = do_merge(STORE, repo, pr)
        if not ok:
            return {"error": str(result)}, True
        return {"sha": result, "merged": True,
                "message": f"{pr.title} (#{pr.number})",
                "number": pr.number}, False
    return {"error": f"unknown tool: {name}"}, True


def _mcp_result(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _mcp_error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}


def handle_mcp(body, host: str):
    """body: parsed JSON. -> (status, response_obj_or_None)."""
    if isinstance(body, list):
        responses = []
        for item in body:
            status, resp = handle_mcp(item, host)
            if resp is not None:
                responses.append(resp)
        return 200, responses
    if not isinstance(body, dict):
        return 400, _mcp_error(None, -32700, "Parse error")
    method = body.get("method")
    request_id = body.get("id")
    is_notification = "id" not in body
    params = body.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    if method == "server/discover":
        result = {
            "supportedVersions": [MCP_REVISION, *MCP_LEGACY_REVISIONS],
            "serverInfo": dict(_MCP_SERVER_INFO),
            "capabilities": {"tools": {}},
            "_meta": {"io.modelcontextprotocol/serverInfo": dict(_MCP_SERVER_INFO)},
        }
        return 200, _mcp_result(request_id, result)
    if method == "initialize":
        requested = params.get("protocolVersion") or MCP_REVISION
        result = {
            "protocolVersion": requested,
            "capabilities": {"tools": {}},
            "serverInfo": dict(_MCP_SERVER_INFO),
        }
        return 200, _mcp_result(request_id, result)
    if method in ("notifications/initialized", "notifications/cancelled",
                  "notifications/progress"):
        return 202, None
    if method == "tools/list":
        return 200, _mcp_result(request_id, {"tools": MCP_TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        if name not in _MCP_TOOL_NAMES:
            return 200, _mcp_error(request_id, -32602, f"unknown tool: {name}")
        try:
            obj, is_error = _mcp_tool_call(name, args, host)
        except KeyError as exc:
            obj = {"error": str(exc)}
            is_error = True
        text = json.dumps(obj, sort_keys=True, separators=(",", ":"))
        result = {"content": [{"type": "text", "text": text}],
                  "structuredContent": obj}
        if is_error:
            result["isError"] = True
        return 200, _mcp_result(request_id, result)
    if method is None:
        return 400, _mcp_error(request_id, -32600, "Invalid Request")
    if is_notification:
        return 202, None
    return 200, _mcp_error(request_id, -32601, f"method not found: {method}")


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

EXPECTED_TOKEN = os.environ.get("MAKEFUN_SHOP_TOKEN")


def _authorized(headers) -> bool:
    auth = headers.get("Authorization") or ""
    if not auth.startswith("Bearer "):
        return False
    token = auth[len("Bearer "):].strip()
    if not token:
        return False
    if EXPECTED_TOKEN:
        return token == EXPECTED_TOKEN
    return True


class ForgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = SERVER_NAME + "/" + VERSION

    def log_message(self, *args):  # silence request logging
        pass

    def setup(self):
        super().setup()
        # One small write per response must not wait for Nagle: the bench
        # does thousands of sequential keep-alive round-trips, and a delayed
        # ACK (~20 ms) per round-trip would dominate everything.
        try:
            import socket as _socket
            self.request.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)
        except OSError:
            pass

    def _read_body(self) -> bytes:
        length = self.headers.get("Content-Length")
        if not length:
            return b""
        try:
            n = int(length)
        except ValueError:
            return b""
        if n <= 0:
            return b""
        return self.rfile.read(n)

    def _send(self, status: int, body: bytes, content_type: str,
              extra: dict | None = None):
        # A single write for the whole response (status line, headers, body):
        # splitting headers and body into two writes lets Nagle + the
        # client's delayed ACK stall every keep-alive round-trip by ~20 ms.
        reason = self.responses.get(status, ("OK",))[0]
        head = [f"HTTP/1.1 {status} {reason}\r\n",
                f"Server: {self.version_string()}\r\n",
                f"Date: {self.date_time_string()}\r\n",
                f"Content-Type: {content_type}\r\n",
                f"Content-Length: {len(body)}\r\n",
                f"X-GitHub-Request-Id: {STORE.next_request_id()}\r\n"]
        if extra:
            for key, value in extra.items():
                head.append(f"{key}: {value}\r\n")
        head.append("\r\n")
        self.wfile.write("".join(head).encode("iso-8859-1") + body)

    def _send_json(self, status: int, obj, extra: dict | None = None):
        if obj is None:
            self._send(status, b"", "application/json", extra)
            return
        self._send(status, json.dumps(obj).encode("utf-8"),
                   "application/json", extra)

    def _dispatch(self, method: str):
        try:
            split = urllib.parse.urlsplit(self.path)
            path = split.path
            query = urllib.parse.parse_qs(split.query)
            host = self.headers.get("Host") or "127.0.0.1"
            base_url = f"http://{host}{path}"
            seg = [urllib.parse.unquote(p) for p in path.split("/") if p != ""]
            body_bytes = self._read_body() if method in ("POST", "PUT", "PATCH", "DELETE") else b""

            # ---- boot probe (no auth) ----
            if seg == ["healthz"] and method == "GET":
                self._send_json(200, {"ok": True})
                return

            # ---- git smart HTTP (no auth) ----
            if len(seg) >= 2 and seg[1].endswith(".git"):
                self._dispatch_git(method, seg, query, body_bytes)
                return

            # ---- JSON APIs (auth required) ----
            if seg[:2] == ["api", "v3"] or seg == ["api", "graphql"] or seg == ["mcp"]:
                if not _authorized(self.headers):
                    self._send_json(401, {"message": "Bad credentials"})
                    return
                if seg == ["mcp"]:
                    if method != "POST":
                        self._send_json(405, {"message": "Method Not Allowed"})
                        return
                    try:
                        parsed = json.loads(body_bytes) if body_bytes else None
                    except ValueError:
                        self._send_json(400, _mcp_error(None, -32700, "Parse error"))
                        return
                    if parsed is None:
                        self._send_json(400, _mcp_error(None, -32700, "Parse error"))
                        return
                    status, resp = handle_mcp(parsed, host)
                    self._send_json(status, resp)
                    return
                if seg == ["api", "graphql"]:
                    if method != "POST":
                        self._send_json(405, {"message": "Method Not Allowed"})
                        return
                    try:
                        parsed = json.loads(body_bytes) if body_bytes else None
                    except ValueError:
                        self._send_json(400, {"message": "Invalid JSON"})
                        return
                    self._send_json(200, handle_graphql(parsed))
                    return
                # REST v3
                try:
                    parsed = json.loads(body_bytes) if body_bytes else {}
                except ValueError:
                    self._send_json(400, {"message": "Invalid JSON"})
                    return
                try:
                    status, extra, obj = handle_rest(method, seg[2:], query,
                                                      parsed, host, base_url)
                except ApiError as exc:
                    self._send_json(exc.status, {"message": exc.message})
                    return
                self._send_json(status, obj, extra)
                return

            self._send_json(404, {"message": "Not Found"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # never take the server down
            import traceback
            traceback.print_exc()
            try:
                self._send_json(500, {"message": f"internal error: {exc}"})
            except Exception:
                pass

    def _dispatch_git(self, method: str, seg, query, body_bytes: bytes):
        owner = seg[0]
        repo_name = seg[1][:-4]
        repo = get_repo_or_none(owner, repo_name)
        if repo is None:
            self._send_json(404, {"message": "Not Found"})
            return
        rest = seg[2:]
        if method == "GET" and rest == ["info", "refs"]:
            service = query.get("service", [None])[0]
            if service not in ("git-upload-pack", "git-receive-pack"):
                self._send_json(400, {"message": "service is required"})
                return
            if service == "git-upload-pack" and \
                    (self.headers.get("Git-Protocol") or "").startswith("version=2"):
                payload = git_v2_advertisement()
            else:
                payload = git_advertisement(repo, service)
            self._send(200, payload,
                       f"application/x-{service}-advertisement")
            return
        if method == "POST" and rest == ["git-upload-pack"]:
            if len(body_bytes) < 4 or not _is_hex4(body_bytes[:4]):
                self._send(400, b"bad pkt-line", "text/plain")
                return
            if (self.headers.get("Git-Protocol") or "").startswith("version=2"):
                packets, _ = parse_pkt_lines(body_bytes)
                command = None
                for kind, payload in packets:
                    if kind == "data":
                        line = payload.decode("utf-8", "replace").strip()
                        if line.startswith("command="):
                            command = line[len("command="):]
                        break
                if command == "ls-refs":
                    args = {"peel": False, "symrefs": False, "unborn": False,
                            "prefixes": []}
                    seen_delim = False
                    for kind, payload in packets:
                        if kind == "delim":
                            seen_delim = True
                        elif kind == "data" and seen_delim:
                            line = payload.decode("utf-8", "replace").strip()
                            if line == "peel":
                                args["peel"] = True
                            elif line == "symrefs":
                                args["symrefs"] = True
                            elif line == "unborn":
                                args["unborn"] = True
                            elif line.startswith("ref-prefix "):
                                args["prefixes"].append(line[len("ref-prefix "):])
                    self._send(200, git_ls_refs(repo, args),
                               "application/x-git-upload-pack-result")
                    return
                if command == "fetch":
                    self._send(200, git_fetch_v2(repo, body_bytes),
                               "application/x-git-upload-pack-result")
                    return
                self._send(400, b"unknown command", "text/plain")
                return
            self._send(200, git_fetch_v0(repo, body_bytes),
                       "application/x-git-upload-pack-result")
            return
        if method == "POST" and rest == ["git-receive-pack"]:
            if len(body_bytes) < 4 or not _is_hex4(body_bytes[:4]):
                self._send(400, b"bad pkt-line", "text/plain")
                return
            self._send(200, git_receive_pack(repo, body_bytes),
                       "application/x-git-receive-pack-result")
            return
        self._send_json(404, {"message": "Not Found"})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")


def _is_hex4(chunk: bytes) -> bool:
    if len(chunk) != 4:
        return False
    try:
        int(chunk.decode("ascii"), 16)
        return True
    except (ValueError, UnicodeDecodeError):
        return False


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------

def _http_json(url: str, method: str = "GET", payload=None, timeout: float = 5.0):
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, json.loads(response.read() or b"{}")


class ForgeServer(ThreadingHTTPServer):
    # The clone benchmark opens 50 connections at once; the default listen
    # backlog of 5 drops SYNs and costs ~1 s retransmits per round.
    request_queue_size = 256
    daemon_threads = True
    allow_reuse_address = True


def main():
    env_url = os.environ.get("MAKEFUN_ENV_URL")
    port = int(os.environ.get("FORGE_PORT", "0"))
    server = ForgeServer(("127.0.0.1", port), ForgeHandler)
    actual_port = server.server_address[1]
    if not env_url:
        print(f"listening on http://127.0.0.1:{actual_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = env_url.rstrip("/")
    deadline = time.time() + 60.0
    delay = 0.2
    while True:
        try:
            status, _ = _http_json(f"{base}/register", "POST", {"port": actual_port})
            if status in (200, 409):
                break
        except Exception:
            pass
        if time.time() > deadline:
            print("register failed: giving up", flush=True)
            return 1
        time.sleep(delay)
        delay = min(delay * 2, 5.0)
    while True:
        try:
            _status, payload = _http_json(f"{base}/done")
            if isinstance(payload, dict) and payload.get("done") is True:
                break
        except Exception:
            pass
        time.sleep(0.5)
    server.shutdown()
    server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
