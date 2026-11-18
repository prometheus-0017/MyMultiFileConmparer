#!/usr/bin/env python3
"""批量文件比较工具：按配置里的编号路径两两比对，列出异常项，输入编号用 WinMerge 打开。

配置文件格式（默认 filecompare.txt，或用命令行参数指定）：

    1 <path1>
    2 <path2>
    ...
    ---
    1,2:<通配的exclude>
    1,3:<通配的exclude>

`---` 之前是带编号的待比较路径（目录或文件均可，目录递归比较）。`---` 之后每行
形如 `1,2:<排除通配>`：逗号分隔的两个编号指定比较哪两侧，冒号后面是可选的排除通配，
多个通配可用逗号/分号/空格分隔。不写比较对时按编号顺序两两全比。

排除通配规则：不含 `/` 时按名字匹配任意层级（`*.log`、`build`）；含 `/` 时匹配相对
路径，`**` 跨目录、`*` 单层、`?` 单字符，命中目录会连带其下全部内容一起排除。

注释用 `#`：可整行注释，也可写在行尾（`#` 前需有空白，因此路径里的 `#` 不受影响，
引号内的 `#` 一律保留）。例：`3 dC  # 备份目录`。

异常以“比较项”（配置里的一个路径对）为粒度，文件差异只作为该比较项的明细缩进列出。
行尾符 CRLF/LF 的差异不计为异常（二进制文件除外），只在明细后附带一句统计。

    异常0: C:\\proj\\a -> C:\\proj\\b    [2 处差异]
            src/main.py    内容不同
            src/util.js    仅存在于前者

输入异常编号即用 WinMerge 打开该比较项的两侧（目录走递归文件夹比较）。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_CONFIG = "filecompare.txt"
CHUNK = 1 << 20
REASON_DIFF = "内容不同"
REASON_EOL = "仅行尾符不同(CRLF/LF)"
REASON_ONLY_A = "仅存在于前者"
REASON_ONLY_B = "仅存在于后者"


# ---------------------------------------------------------------- 配置解析

def _split_kv(line: str) -> tuple[str, str]:
    """`1 <path>` / `1: <path>` / `1=<path>` / `1|<path>` -> ("1", "<path>")"""
    m = re.match(r"^\s*(\d+)\s*[:=|]\s*(.*)$", line)
    if m:
        return m.group(1), m.group(2)
    m = re.match(r"^\s*(\d+)\s+(.*)$", line)
    if m:
        return m.group(1), m.group(2)
    return "", line.strip()


def strip_quotes(value: str) -> str:
    """去掉路径首尾成对的引号，兼容配置里写 "带空格的路径" 的写法。"""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def strip_comment(line: str) -> str:
    """去掉行尾 `#` 注释：`#` 只在行首或前面是空白时才算注释，引号内的一律保留。"""
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i].rstrip()
    return line.rstrip()


def parse_config(text: str) -> tuple[dict[int, str], list[tuple[int, int, str]]]:
    paths: dict[int, str] = {}
    pairs: list[tuple[int, int, str]] = []
    section = 0

    for raw in text.splitlines():
        line = strip_comment(raw.strip())
        if not line:
            continue
        if line.startswith("---"):
            section = 1
            continue

        if section == 0:
            num, value = _split_kv(line)
            if not value:
                raise ValueError(f"无法解析路径行：{raw!r}")
            if num:
                key = int(num)
            else:
                key = max(paths, default=0) + 1
            if key in paths:
                raise ValueError(f"编号 {key} 重复：{raw!r}")
            paths[key] = value
        else:
            head, _, excludes = line.partition(":")
            nums = [int(n) for n in re.findall(r"\d+", head)]
            if len(nums) < 2:
                raise ValueError(f"无法解析比较对行（需要两个编号）：{raw!r}")
            for a, b in zip(nums, nums[1:]):
                pairs.append((a, b, excludes))

    if not paths:
        raise ValueError("配置中没有路径条目")
    if not pairs:
        keys = sorted(paths)
        pairs = [(a, b, "") for i, a in enumerate(keys) for b in keys[i + 1:]]
    return paths, pairs


# ---------------------------------------------------------------- 通配排除

class Matcher:
    """单个通配规则：不含 `/` 时匹配任意层级的文件名或目录名，含 `/` 时匹配相对路径。"""

    def __init__(self, pattern: str):
        self.pattern = pattern.replace("\\", "/").strip("/")
        self.base_only = "/" not in self.pattern
        self.literal = self.base_only and not re.search(r"[*?\[\]]", self.pattern)
        self.rx = re.compile(self._translate(self.pattern)) if not self.literal else None

    @staticmethod
    def _translate(pattern: str) -> str:
        out, i = [], 0
        while i < len(pattern):
            c = pattern[i]
            if pattern.startswith("**", i):
                out.append(".*")
                i += 2
                if pattern[i: i + 1] == "/":
                    i += 1
            elif c == "*":
                out.append("[^/]*")
                i += 1
            elif c == "?":
                out.append("[^/]")
                i += 1
            elif c == "[":
                j = pattern.find("]", i + 1)
                if j < 0:
                    out.append(re.escape(c))
                    i += 1
                else:
                    out.append("[" + pattern[i + 1:j] + "]")
                    i = j + 1
            else:
                out.append(re.escape(c))
                i += 1
        return "".join(out)

    def hit(self, rel: str) -> bool:
        parts = rel.split("/")
        if self.base_only:
            if self.literal:
                return self.pattern in parts
            return self._test(parts[-1])
        for i in range(1, len(parts) + 1):
            if self._test("/".join(parts[:i])):
                return True
        return False

    def _test(self, value: str) -> bool:
        return self.rx is not None and self.rx.fullmatch(value) is not None


def build_matchers(patterns: str) -> list[Matcher]:
    matchers = []
    for token in re.split(r"[,;、\s]+", patterns or ""):
        token = token.strip()
        if token and token != "*":
            matchers.append(Matcher(token))
    return matchers


def is_excluded(rel: str, matchers: list[Matcher]) -> bool:
    return any(m.hit(rel) for m in matchers)


# ---------------------------------------------------------------- 文件收集与比较

def collect(root: str) -> dict[str, tuple[str, str]]:
    """返回 {规范化相对路径: (展示用相对路径, 绝对路径)}；目录递归，文件输入只有一项。"""
    p = Path(root)
    if not p.exists():
        raise FileNotFoundError(root)
    if p.is_file():
        rel = p.name.replace("\\", "/")
        return {os.path.normcase(rel): (rel, str(p.resolve()))}
    files: dict[str, tuple[str, str]] = {}
    base = p.resolve()
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
        for name in filenames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full) and not os.path.exists(full):
                continue
            rel = os.path.relpath(full, base).replace("\\", "/")
            files[os.path.normcase(rel)] = (rel, full)
    return files


def digest(path: str, transform=None) -> str:
    h = hashlib.sha256()
    carry = b""
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            if transform is None:
                h.update(chunk)
                continue
            data = carry + chunk
            # 结尾的 \r 可能与下一块的 \n 组成 \r\n，先扣住
            carry = b"\r" if data.endswith(b"\r") else b""
            if carry:
                data = data[:-1]
            h.update(transform(data))
    if carry:
        h.update(transform(b"\r"))
    return h.hexdigest()


def normalize_eol(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def is_binary(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return b"\x00" in fh.read(8192)
    except OSError:
        return True


def diff_reason(a: str, b: str) -> str | None:
    """None 表示一致；行尾符归一化后相同则返回 REASON_EOL。"""
    try:
        if os.path.getsize(a) == os.path.getsize(b) and digest(a) == digest(b):
            return None
        if is_binary(a) or is_binary(b):
            return REASON_DIFF
        if digest(a, normalize_eol) == digest(b, normalize_eol):
            return REASON_EOL
        return REASON_DIFF
    except OSError:
        return REASON_DIFF


class Anomaly:
    """一个比较项（配置里的路径对）的异常结果，files 是其文件级明细。"""

    __slots__ = ("a", "b", "label", "excludes", "files", "eol")

    def __init__(self, a: str, b: str, label: str, excludes: str,
                 files: list[tuple[str, str]], eol: list[str]):
        self.a, self.b, self.label, self.excludes, self.files, self.eol = \
            a, b, label, excludes, files, eol


def compare_pair(paths: dict[int, str], num_a: int, num_b: int,
                 excludes: str) -> Anomaly:
    root_a, root_b = paths[num_a], paths[num_b]
    matchers = build_matchers(excludes)
    both_files = os.path.isfile(root_a) and os.path.isfile(root_b)
    if both_files:
        # 单文件对单文件：两侧文件名可能不同，仍算同一个比较项
        fa = {"": (os.path.basename(root_a), os.path.abspath(root_a))}
        fb = {"": (os.path.basename(root_b), os.path.abspath(root_b))}
    else:
        fa = {k: v for k, v in collect(root_a).items() if not is_excluded(v[0], matchers)}
        fb = {k: v for k, v in collect(root_b).items() if not is_excluded(v[0], matchers)}

    files: list[tuple[str, str]] = []
    eol: list[str] = []
    for key in sorted(set(fa) | set(fb)):
        rel_a, a = fa.get(key, (None, None))
        rel_b, b = fb.get(key, (None, None))
        if a is None:
            files.append((rel_b, REASON_ONLY_B))
        elif b is None:
            files.append((rel_a, REASON_ONLY_A))
        else:
            reason = diff_reason(a, b)
            if reason is None:
                continue
            if reason == REASON_EOL:
                eol.append(rel_a)
            else:
                files.append((rel_a, reason))
    label = f"[{num_a}] vs [{num_b}]"
    return Anomaly(root_a, root_b, label, excludes.strip(), files, eol)


# ---------------------------------------------------------------- WinMerge

def find_winmerge(explicit: str | None) -> str | None:
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    if os.environ.get("WINMERGE"):
        candidates.append(os.environ["WINMERGE"])
    for name in ("WinMergeU", "WinMerge", "winmergeu", "winmerge"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        root = os.environ.get(env)
        if root:
            candidates.append(os.path.join(root, "WinMerge", "WinMergeU.exe"))
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def open_winmerge(exe: str, a: str, b: str) -> None:
    subprocess.Popen([exe, "/e", "/r", a, b])


# ---------------------------------------------------------------- 输出

def report(anomalies: list[Anomaly]) -> None:
    if not anomalies:
        print("无异常：所有比较项的文件均一致。")
        return
    for i, an in enumerate(anomalies):
        excludes = f"    排除 {an.excludes}" if an.excludes else ""
        print(f"\n异常{i}:{an.a} -> {an.b}    [{len(an.files)} 处差异]{excludes}")
        for rel, reason in an.files:
            print(f"        {rel}    {reason}")
        if an.eol:
            print(f"        （另有 {len(an.eol)} 处仅行尾符不同，已视为一致）")
    print(f"\n共 {len(anomalies)} 个异常比较项。")


def interactive(anomalies: list[Anomaly], exe: str) -> None:
    seen: set[int] = set()
    while True:
        try:
            answer = input(f"输入异常编号(0-{len(anomalies) - 1})打开 WinMerge 比较该比较项，"
                           f"回车重列表，q 退出 > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if answer in ("q", "quit", "exit"):
            return
        if not answer:
            report(anomalies)
            continue
        if not answer.lstrip("+").isdigit():
            print("请输入数字编号或 q。")
            continue
        idx = int(answer)
        if not 0 <= idx < len(anomalies):
            print(f"编号超出范围，有效范围 0-{len(anomalies) - 1}。")
            continue
        an = anomalies[idx]
        open_winmerge(exe, an.a, an.b)
        print(f"已启动 WinMerge 比较异常{idx}（{an.label}）"
              f"{'，此前已打开过' if idx in seen else ''}。")
        seen.add(idx)


def usage(code: int) -> int:
    print(__doc__)
    print("用法: python filecompare.py [配置文件] [-n] [--winmerge 路径]")
    print("  配置文件默认 filecompare.txt；'-' 表示从标准输入读取")
    print("  -n 只输出结果不进入交互；--winmerge 指定 WinMergeU.exe")
    return code


def read_config(path: str) -> str:
    """按字节读配置再试 UTF-8 / GBK 解码，兼容管道传入和记事本另存的 ANSI 文件。"""
    data = sys.stdin.buffer.read() if path == "-" else Path(path).read_bytes()
    for enc in ("utf-8-sig", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        if not stream.isatty():  # 重定向到非中文语系的管道/文件时避免编码报错
            try:
                stream.reconfigure(encoding="utf-8", errors="backslashreplace")
            except (OSError, ValueError):
                pass

    config = DEFAULT_CONFIG
    batch = False
    winmerge_arg = None
    args = argv[1:]
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-h", "--help"):
            return usage(0)
        if a in ("-n", "--no-input"):
            batch = True
        elif a in ("--winmerge", "-w"):
            i += 1
            if i >= len(args):
                print("缺少 --winmerge 的参数值", file=sys.stderr)
                return 2
            winmerge_arg = args[i]
        elif a.startswith("--winmerge="):
            winmerge_arg = a.split("=", 1)[1]
        elif a.startswith("-") and a != "-":
            print(f"未知选项：{a}", file=sys.stderr)
            return 2
        else:
            config = a
        i += 1

    try:
        text = read_config(config)
        paths, pairs = parse_config(text)
    except (OSError, ValueError) as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    missing = [k for k in set(n for p in pairs for n in p[:2]) if k not in paths]
    if missing:
        print(f"配置错误：比较对引用了未定义的编号 {sorted(missing)}", file=sys.stderr)
        return 2

    paths = {k: os.path.abspath(strip_quotes(v)) for k, v in paths.items()}

    for k, v in sorted(paths.items()):
        print(f"[{k}] {v}{'' if Path(v).exists() else '    <不存在>'}")

    anomalies: list[Anomaly] = []
    for num_a, num_b, excludes in pairs:
        if not Path(paths[num_a]).exists() or not Path(paths[num_b]).exists():
            print(f"\n[{num_a}] vs [{num_b}] 跳过：路径不存在")
            continue
        try:
            item = compare_pair(paths, num_a, num_b, excludes)
        except OSError as exc:
            print(f"\n[{num_a}] vs [{num_b}] 比较失败：{exc}", file=sys.stderr)
            continue
        if item.files:
            anomalies.append(item)
        else:
            tail = f"（{len(item.eol)} 处仅行尾符不同，已视为一致）" if item.eol else ""
            print(f"\n[{num_a}] vs [{num_b}] 一致{tail}")

    report(anomalies)

    if batch or not anomalies or not sys.stdin.isatty():
        return 1 if anomalies else 0

    exe = find_winmerge(winmerge_arg)
    if not exe:
        print("未找到 WinMerge，可用 --winmerge <路径> 或设置环境变量 WINMERGE 指定。",
              file=sys.stderr)
        return 1
    print(f"WinMerge: {exe}")
    interactive(anomalies, exe)
    return 1 if anomalies else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
