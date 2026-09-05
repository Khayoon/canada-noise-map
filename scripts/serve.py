#!/usr/bin/env python3
"""
Tiny static server for local development with HTTP Range support.

PMTiles works by fetching byte ranges of one big file, and Python's built-in
`http.server` does not understand Range requests, so use this instead:

    python scripts/serve.py            # serves ./web on http://localhost:8000
    python scripts/serve.py 8080 web   # custom port / directory
"""
import os
import re
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


class RangeHandler(SimpleHTTPRequestHandler):
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".mjs": "text/javascript",
        ".js": "text/javascript",
        ".json": "application/json",
        ".pmtiles": "application/octet-stream",
    }

    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def send_head(self):
        path = self.translate_path(self.path)
        rng = self.headers.get("Range")
        if not rng or os.path.isdir(path) or not os.path.exists(path):
            return super().send_head()
        m = RANGE_RE.match(rng)
        if not m:
            return super().send_head()
        size = os.path.getsize(path)
        start = int(m.group(1)) if m.group(1) else None
        end = int(m.group(2)) if m.group(2) else None
        if start is None:  # suffix range: last N bytes
            start = max(size - (end or 0), 0)
            end = size - 1
        else:
            end = min(end if end is not None else size - 1, size - 1)
        if start > end or start >= size:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return None
        f = open(path, "rb")
        f.seek(start)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        self._range_len = end - start + 1
        return f

    def copyfile(self, source, outputfile):
        n = getattr(self, "_range_len", None)
        if n is None:
            return super().copyfile(source, outputfile)
        remaining = n
        while remaining > 0:
            chunk = source.read(min(1 << 16, remaining))
            if not chunk:
                break
            outputfile.write(chunk)
            remaining -= len(chunk)
        self._range_len = None

    def log_message(self, fmt, *args):
        if "-q" in sys.argv:
            return
        super().log_message(fmt, *args)


def main():
    args = [a for a in sys.argv[1:] if a != "-q"]
    port = int(args[0]) if args else 8000
    directory = args[1] if len(args) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")
    os.chdir(directory)
    srv = ThreadingHTTPServer(("0.0.0.0", port), RangeHandler)
    print(f"Serving {os.path.abspath(directory)} on http://localhost:{port}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
