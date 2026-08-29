from .cli import main
import argparse
from .mcp_server import serve
from .http_server import serve_http


if __name__ == "__main__":
    if len(__import__("sys").argv) > 1 and __import__("sys").argv[1] == "mcp":
        parser = argparse.ArgumentParser(); parser.add_argument("--state", required=True)
        serve(parser.parse_args(__import__("sys").argv[2:]).state)
    elif len(__import__("sys").argv) > 1 and __import__("sys").argv[1] == "http":
        parser = argparse.ArgumentParser(); parser.add_argument("--state", required=True); parser.add_argument("--host", default="127.0.0.1"); parser.add_argument("--port", type=int, default=8765)
        args=parser.parse_args(__import__("sys").argv[2:]); serve_http(args.state, args.host, args.port)
    else:
        raise SystemExit(main())
