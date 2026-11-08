"""PDF text extractor v2: balanced dict parsing + text matrix tracking + line grouping."""
import re, sys, zlib, io, os

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

OBJ_RE = re.compile(rb"(?<![0-9])(\d+)\s+(\d+)\s+obj\b")


def parse_objects(data):
    objs = {}
    for m in OBJ_RE.finditer(data):
        num = int(m.group(1))
        start = m.end()
        end = data.find(b"endobj", start)
        if end == -1:
            continue
        objs[num] = data[start:end]
    return objs


def get_stream(body):
    i = body.find(b"stream")
    if i == -1:
        return None
    dict_part = body[:i]
    j = i + 6
    if body[j:j + 2] == b"\r\n":
        j += 2
    elif body[j:j + 1] in (b"\n", b"\r"):
        j += 1
    k = body.rfind(b"endstream")
    raw = body[j:k] if k != -1 else body[j:]
    if b"FlateDecode" in dict_part:
        for attempt in (raw, raw.rstrip(b"\r\n")):
            try:
                return zlib.decompress(attempt)
            except zlib.error:
                continue
        try:
            d = zlib.decompressobj()
            return d.decompress(raw)
        except zlib.error:
            return None
    return raw


def scan_balanced(body, start):
    """body[start:] begins with '<<'; return index just past matching '>>'."""
    depth = 0
    i = start
    n = len(body)
    while i < n - 1:
        if body[i:i + 2] == b"<<":
            depth += 1
            i += 2
        elif body[i:i + 2] == b">>":
            depth -= 1
            i += 2
            if depth == 0:
                return i
        elif body[i:i + 1] == b"(":
            # skip literal string
            d = 1
            i += 1
            while i < n and d > 0:
                if body[i:i + 1] == b"\\":
                    i += 2
                    continue
                if body[i:i + 1] == b"(":
                    d += 1
                elif body[i:i + 1] == b")":
                    d -= 1
                i += 1
        else:
            i += 1
    return -1


def resolve_dict_key(body, key, objs, depth=0):
    """Find /key then return the raw dict body (resolving indirect refs)."""
    if depth > 6:
        return b""
    m = re.search(rb"/" + key + rb"\b", body)
    if not m:
        return b""
    i = m.end()
    while i < len(body) and body[i:i + 1] in b" \r\n\t":
        i += 1
    if body[i:i + 2] == b"<<":
        end = scan_balanced(body, i)
        return body[i:end] if end != -1 else body[i:]
    r = re.match(rb"(\d+)\s+\d+\s+R", body[i:])
    if r:
        return objs.get(int(r.group(1)), b"")
    return b""


def parse_tounicode(cmap_bytes):
    mapping = {}
    for block in re.findall(rb"beginbfchar(.*?)endbfchar", cmap_bytes, re.S):
        for src, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", block):
            try:
                mapping[int(src, 16)] = bytes.fromhex(dst.decode()).decode("utf-16-be", "ignore")
            except Exception:
                pass
    for block in re.findall(rb"beginbfrange(.*?)endbfrange", cmap_bytes, re.S):
        for lo, hi, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            a, b, base = int(lo, 16), int(hi, 16), bytes.fromhex(dst.decode())
            for off, code in enumerate(range(a, min(b, a + 65535) + 1)):
                try:
                    v = int.from_bytes(base, "big") + off
                    mapping[code] = v.to_bytes(2, "big").decode("utf-16-be", "ignore")
                except Exception:
                    break
        for lo, hi, arr in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\[(.*?)\]", block, re.S):
            a = int(lo, 16)
            for off, dst in enumerate(re.findall(rb"<([0-9A-Fa-f]*)>", arr)):
                try:
                    mapping[a + off] = bytes.fromhex(dst.decode()).decode("utf-16-be", "ignore")
                except Exception:
                    pass
    return mapping


def parse_codespace(cmap_bytes):
    out = []
    for block in re.findall(rb"begincodespacerange(.*?)endcodespacerange", cmap_bytes, re.S):
        for lo, hi in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            out.append((len(lo) // 2, int(lo, 16), int(hi, 16)))
    return out


def unescape_pdf_string(s):
    out = bytearray()
    i = 0
    while i < len(s):
        c = s[i]
        if c == 0x5C:
            i += 1
            if i >= len(s):
                break
            n = s[i]
            table = {0x6E: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12, 0x28: 0x28, 0x29: 0x29, 0x5C: 0x5C}
            if n in table:
                out.append(table[n]); i += 1
            elif 0x30 <= n <= 0x37:
                digs = bytearray()
                while i < len(s) and len(digs) < 3 and 0x30 <= s[i] <= 0x37:
                    digs.append(s[i]); i += 1
                out.append(int(digs.decode(), 8) & 0xFF)
            elif n == 0x0A:
                i += 1
            else:
                out.append(n); i += 1
        else:
            out.append(c); i += 1
    return bytes(out)


def decode_text_bytes(raw, mapping, codespaces):
    if not mapping:
        return raw.decode("latin-1", "replace")
    lens = sorted({cs[0] for cs in codespaces}) or [2]
    if 2 in lens or max(mapping) > 255:
        nbytes = 2
    else:
        nbytes = lens[0]
    out = []
    for i in range(0, len(raw) - nbytes + 1, nbytes):
        code = int.from_bytes(raw[i:i + nbytes], "big")
        out.append(mapping.get(code, ""))
    return "".join(out)


NUM = rb"[+-]?(?:\d+\.\d*|\.\d+|\d+)"


def tokenize_content(content):
    """Yield (op, args) for text-relevant operators."""
    i, n = 0, len(content)
    args = []
    while i < n:
        c = content[i:i + 1]
        if c in b" \r\n\t\x00\x0c":
            i += 1
            continue
        if c == b"%":
            j = content.find(b"\n", i)
            i = n if j == -1 else j + 1
            continue
        if c == b"(":
            d = 1
            j = i + 1
            while j < n and d > 0:
                ch = content[j:j + 1]
                if ch == b"\\":
                    j += 2
                    continue
                if ch == b"(":
                    d += 1
                elif ch == b")":
                    d -= 1
                j += 1
            args.append(("str", unescape_pdf_string(content[i + 1:j - 1])))
            i = j
            continue
        if c == b"<":
            if content[i:i + 2] == b"<<":
                i += 2
                continue
            j = content.find(b">", i)
            if j == -1:
                break
            hx = re.sub(rb"\s", b"", content[i + 1:j])
            if len(hx) % 2:
                hx += b"0"
            try:
                args.append(("str", bytes.fromhex(hx.decode())))
            except ValueError:
                pass
            i = j + 1
            continue
        if c == b"/":
            m = re.match(rb"/([^\s/<>\[\]()%{}]*)", content[i:])
            args.append(("name", b"/" + m.group(1)))
            i += m.end()
            continue
        if c == b"[" or c == b"]":
            i += 1
            continue
        m = re.match(NUM, content[i:])
        if m and m.group(0):
            try:
                args.append(("num", float(m.group(0))))
            except ValueError:
                pass
            i += m.end()
            continue
        m = re.match(rb"[A-Za-z'\"*]+", content[i:])
        if m:
            op = m.group(0)
            if op == b"BI":
                k = content.find(b"EI", i)
                i = n if k == -1 else k + 2
                args = []
                continue
            yield op, args
            args = []
            i += m.end()
            continue
        i += 1


def extract(path, out_path):
    data = open(path, "rb").read()
    objs = parse_objects(data)
    pages = sorted(num for num, body in objs.items() if re.search(rb"/Type\s*/Page[^s]", body))

    all_pages = []
    for pno in pages:
        body = objs[pno]
        font_map = {}
        res = resolve_dict_key(body, b"Resources", objs)
        fdict = resolve_dict_key(res, b"Font", objs) if res else b""
        for name, ref in re.findall(rb"/([^\s/<>\[\]()%{}]+)\s+(\d+)\s+0\s+R", fdict):
            fb = objs.get(int(ref), b"")
            tu = re.search(rb"/ToUnicode\s+(\d+)\s+0\s+R", fb)
            if tu:
                st = get_stream(objs.get(int(tu.group(1)), b""))
                if st:
                    font_map[b"/" + name] = (parse_tounicode(st), parse_codespace(st))

        content = b""
        m = re.search(rb"/Contents\s+(\d+)\s+0\s+R", body)
        if m:
            content = get_stream(objs.get(int(m.group(1)), b"")) or b""
        else:
            arr = resolve_dict_key(body, b"Contents", objs, depth=1)
            for r in re.findall(rb"(\d+)\s+0\s+R", arr):
                content += (get_stream(objs.get(int(r), b"")) or b"") + b"\n"

        font = (None, [])
        IDENT = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
        ctm = IDENT
        stack = []
        tlm = IDENT
        leading = 0.0
        runs = []  # (dev_y, dev_x, text)

        def mul(m1, m2):
            a1, b1, c1, d1, e1, f1 = m1
            a2, b2, c2, d2, e2, f2 = m2
            return (a1 * a2 + b1 * c2, a1 * b2 + b1 * d2,
                    c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
                    e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2)

        def emit(txt):
            if not txt:
                return
            a, b, c, d, e, f = mul(tlm, ctm)
            runs.append((round(f, 1), round(e, 1), txt))

        for op, a in tokenize_content(content):
            if op == b"q":
                stack.append(ctm)
            elif op == b"Q":
                ctm = stack.pop() if stack else IDENT
            elif op == b"cm":
                nums = [v for t, v in a if t == "num"]
                if len(nums) >= 6:
                    ctm = mul(tuple(nums[:6]), ctm)
            elif op == b"BT":
                tlm = IDENT
            elif op == b"Tf":
                nm = next((v for t, v in a if t == "name"), None)
                if nm:
                    font = font_map.get(nm, (None, []))
            elif op == b"Tm":
                nums = [v for t, v in a if t == "num"]
                if len(nums) >= 6:
                    tlm = tuple(nums[:6])
            elif op in (b"Td", b"TD"):
                nums = [v for t, v in a if t == "num"]
                if len(nums) >= 2:
                    tlm = mul((1, 0, 0, 1, nums[0], nums[1]), tlm)
                    if op == b"TD":
                        leading = -nums[1]
            elif op == b"TL":
                nums = [v for t, v in a if t == "num"]
                if nums:
                    leading = nums[0]
            elif op == b"T*":
                tlm = mul((1, 0, 0, 1, 0, -leading), tlm)
            elif op in (b"Tj", b"'", b'"'):
                if op in (b"'", b'"'):
                    tlm = mul((1, 0, 0, 1, 0, -leading), tlm)
                s = next((v for t, v in a if t == "str"), b"")
                emit(decode_text_bytes(s, font[0], font[1]))
            elif op == b"TJ":
                buf = []
                for t, v in a:
                    if t == "str":
                        buf.append(decode_text_bytes(v, font[0], font[1]))
                    elif t == "num":
                        if v < -120:
                            buf.append(" ")
                emit("".join(buf))

        # group into lines by device y, then order left-to-right within a line
        lines = []
        for yy, xx, txt in runs:
            for ln in lines:
                if abs(ln[0] - yy) <= 2.5:
                    ln[1].append((xx, txt))
                    break
            else:
                lines.append([yy, [(xx, txt)]])
        lines.sort(key=lambda l: -l[0])
        page_text = []
        for yy, parts in lines:
            parts.sort(key=lambda p: p[0])
            joined = "".join(p[1] for p in parts)
            joined = re.sub(r"[ \t]{2,}", " ", joined).strip()
            if joined:
                page_text.append(joined)
        all_pages.append(f"\n\n===== PAGE {len(all_pages) + 1} =====\n" + "\n".join(page_text))

    result = "\n".join(all_pages)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(result)
    print(f"{os.path.basename(path)}: {len(pages)} pages, {len(result)} chars -> {out_path}")


if __name__ == "__main__":
    extract(sys.argv[1], sys.argv[2])
