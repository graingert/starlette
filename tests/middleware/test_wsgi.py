import sys
from collections.abc import Callable, Iterable, Iterator
from typing import Any

import pytest

from starlette.middleware.wsgi import WSGIMiddleware, build_environ
from starlette.types import Message, Scope
from tests.types import TestClientFactory

WSGIResponse = Iterable[bytes]
StartResponse = Callable[..., Any]
Environment = dict[str, Any]


def hello_world(
    environ: Environment,
    start_response: StartResponse,
) -> WSGIResponse:
    status = "200 OK"
    output = b"Hello World!\n"
    headers = [
        ("Content-Type", "text/plain; charset=utf-8"),
        ("Content-Length", str(len(output))),
    ]
    start_response(status, headers)
    return [output]


def echo_body(
    environ: Environment,
    start_response: StartResponse,
) -> WSGIResponse:
    status = "200 OK"
    output = environ["wsgi.input"].read()
    headers = [
        ("Content-Type", "text/plain; charset=utf-8"),
        ("Content-Length", str(len(output))),
    ]
    start_response(status, headers)
    return [output]


def raise_exception(
    environ: Environment,
    start_response: StartResponse,
) -> WSGIResponse:
    raise RuntimeError("Something went wrong")


def return_exc_info(
    environ: Environment,
    start_response: StartResponse,
) -> WSGIResponse:
    try:
        raise RuntimeError("Something went wrong")
    except RuntimeError:
        status = "500 Internal Server Error"
        output = b"Internal Server Error"
        headers = [
            ("Content-Type", "text/plain; charset=utf-8"),
            ("Content-Length", str(len(output))),
        ]
        start_response(status, headers, exc_info=sys.exc_info())
        return [output]


def test_wsgi_get(test_client_factory: TestClientFactory) -> None:
    app = WSGIMiddleware(hello_world)
    client = test_client_factory(app)
    response = client.get("/")
    assert response.status_code == 200
    assert response.text == "Hello World!\n"


def test_wsgi_post(test_client_factory: TestClientFactory) -> None:
    app = WSGIMiddleware(echo_body)
    client = test_client_factory(app)
    response = client.post("/", json={"example": 123})
    assert response.status_code == 200
    assert response.text == '{"example":123}'


def test_wsgi_exception(test_client_factory: TestClientFactory) -> None:
    # Note that we're testing the WSGI app directly here.
    # The HTTP protocol implementations would catch this error and return 500.
    app = WSGIMiddleware(raise_exception)
    client = test_client_factory(app)
    with pytest.raises(RuntimeError):
        client.get("/")


def test_wsgi_exc_info(test_client_factory: TestClientFactory) -> None:
    # Note that we're testing the WSGI app directly here.
    # The HTTP protocol implementations would catch this error and return 500.
    app = WSGIMiddleware(return_exc_info)
    client = test_client_factory(app)
    with pytest.raises(RuntimeError):
        response = client.get("/")

    app = WSGIMiddleware(return_exc_info)
    client = test_client_factory(app, raise_server_exceptions=False)
    response = client.get("/")
    assert response.status_code == 500
    assert response.text == "Internal Server Error"


@pytest.mark.anyio
async def test_wsgi_client_disconnect_on_send() -> None:
    class ServerDisconnectError(OSError):
        pass

    error = ServerDisconnectError("Disconnected")

    def stream_forever(environ: Environment, start_response: StartResponse) -> Iterator[bytes]:
        start_response("200 OK", [("Content-Type", "text/plain; charset=utf-8")])
        while True:
            yield b"chunk"

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        # Simulate an ASGI spec 2.4 server whose client disconnected mid-response.
        if message["type"] == "http.response.body":
            raise error

    scope: Scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "root_path": "",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1453),
        "server": ("testserver", 80),
        "asgi": {"spec_version": "2.4"},
    }
    with pytest.raises(ServerDisconnectError) as exc:
        await WSGIMiddleware(stream_forever)(scope, receive, send)

    # The server's error must propagate unchanged, not wrapped in an ExceptionGroup.
    assert exc.value is error


def test_build_environ() -> None:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/sub/",
        "root_path": "/sub",
        "query_string": b"a=123&b=456",
        "headers": [
            (b"host", b"www.example.org"),
            (b"content-type", b"application/json"),
            (b"content-length", b"18"),
            (b"accept", b"application/json"),
            (b"accept", b"text/plain"),
        ],
        "client": ("134.56.78.4", 1453),
        "server": ("www.example.org", 443),
    }
    body = b'{"example":"body"}'
    environ = build_environ(scope, body)
    stream = environ.pop("wsgi.input")
    assert stream.read() == b'{"example":"body"}'
    assert environ == {
        "CONTENT_LENGTH": "18",
        "CONTENT_TYPE": "application/json",
        "HTTP_ACCEPT": "application/json,text/plain",
        "HTTP_HOST": "www.example.org",
        "PATH_INFO": "/",
        "QUERY_STRING": "a=123&b=456",
        "REMOTE_ADDR": "134.56.78.4",
        "REQUEST_METHOD": "GET",
        "SCRIPT_NAME": "/sub",
        "SERVER_NAME": "www.example.org",
        "SERVER_PORT": 443,
        "SERVER_PROTOCOL": "HTTP/1.1",
        "wsgi.errors": sys.stdout,
        "wsgi.multiprocess": True,
        "wsgi.multithread": True,
        "wsgi.run_once": False,
        "wsgi.url_scheme": "https",
        "wsgi.version": (1, 0),
    }


def test_build_environ_encoding() -> None:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "path": "/小星",
        "root_path": "/中国",
        "query_string": b"a=123&b=456",
        "headers": [],
    }
    environ = build_environ(scope, b"")
    assert environ["SCRIPT_NAME"] == "/中国".encode().decode("latin-1")
    assert environ["PATH_INFO"] == "/小星".encode().decode("latin-1")
