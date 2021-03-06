import asyncio
from functools import partial
from typing import Callable, Optional, TYPE_CHECKING

from .datastructures import CIMultiDict
from .wrappers import Request, Response, Websocket  # noqa: F401

if TYPE_CHECKING:
    from .app import Quart  # noqa: F401


class ASGIHTTPConnection:

    def __init__(self, app: 'Quart', scope: dict) -> None:
        self.app = app
        self.scope = scope
        self.request: Optional[Request] = None
        self.task: Optional[asyncio.Future] = None

    async def __call__(self, receive: Callable, send: Callable) -> None:
        while True:
            event = await receive()
            if event['type'] == 'http.request':
                if self.request is None:
                    headers = CIMultiDict()
                    headers['Remote-Addr'] = self.scope['client'][0]
                    if self.scope['http_version'] < '1.1':
                        headers.setdefault('host', self.app.config['SERVER_NAME'] or '')
                    for name, value in self.scope['headers']:
                        headers.add(name.decode().title(), value.decode())

                    self.request = self.app.request_class(
                        self.scope['method'], self.scope['scheme'],
                        f"{self.scope['path']}?{self.scope['query_string'].decode()}", headers,
                        max_content_length=self.app.config['MAX_CONTENT_LENGTH'],
                        body_timeout=self.app.config['BODY_TIMEOUT'],
                    )
                    # It is important that the app handles the request in a unique
                    # task as the globals are task locals
                    self.task = asyncio.ensure_future(self._handle_request(send))
                self.request.body.append(event['body'])
                if not event.get('more_body', False):
                    self.request.body.set_complete()
            elif event['type'] == 'http.disconnect':
                self.task.cancel()
                break

    async def _handle_request(self, send: Callable) -> None:
        response = await self.app.handle_request(self.request)
        try:
            await asyncio.wait_for(self._send_response(send, response), timeout=response.timeout)
        except asyncio.TimeoutError:
            pass

    async def _send_response(self, send: Callable, response: Response) -> None:
        headers = [
            (key.lower().encode(), value.lower().encode())
            for key, value in response.headers.items()
        ]
        await send({
            'type': 'http.response.start',
            'status': response.status_code,
            'headers': headers,
        })

        if 'http.response.push' in self.scope.get('extensions', {}):
            for path in response.push_promises:
                await send({
                    'type': 'http.response.push',
                    'path': path,
                    'headers': [],
                })

        async for data in response.response:
            await send({
                'type': 'http.response.body',
                'body': data,
                'more_body': True,
            })
        await send({
            'type': 'http.response.body',
            'body': b'',
            'more_body': False,
        })


class ASGIWebsocketConnection:

    def __init__(self, app: 'Quart', scope: dict) -> None:
        self.app = app
        self.scope = scope
        self.queue: asyncio.Queue = asyncio.Queue()
        self.task: Optional[asyncio.Future] = None
        self._accepted = False

    async def __call__(self, receive: Callable, send: Callable) -> None:
        while True:
            event = await receive()
            if event['type'] == 'websocket.connect':
                headers = CIMultiDict()
                headers['Remote-Addr'] = self.scope['client'][0]
                for name, value in self.scope['headers']:
                    headers.add(name.decode().title(), value.decode())

                websocket = self.app.websocket_class(
                    f"{self.scope['path']}?{self.scope['query_string']}", self.scope['scheme'],
                    headers, self.queue, partial(self.send_data, send),
                    partial(self.accept_connection, send),
                )
                self.task = asyncio.ensure_future(self.handle_websocket(websocket, send))
            elif event['type'] == 'websocket.receive':
                await self.queue.put(event.get('bytes') or event['text'])
            elif event['type'] == 'websocket.disconnect':
                self.task.cancel()

    async def handle_websocket(self, websocket: Websocket, send: Callable) -> None:
        response = await self.app.handle_websocket(websocket)
        if (
                response is not None and not self._accepted
                and 'websocket.http.response' in self.scope.get('extensions', {})
        ):
            headers = [
                (key.lower().encode(), value.lower().encode())
                for key, value in response.headers.items()
            ]
            await send({
                'type': 'websocket.http.response.start',
                'status': response.status_code,
                'headers': headers,
            })
            async for data in response.response:
                await send({
                    'type': 'websocket.http.response.body',
                    'body': data,
                    'more_body': True,
                })
            await send({
                'type': 'websocket.http.response.body',
                'body': b'',
                'more_body': False,
            })
        elif self._accepted:
            await send({
                'type': 'websocket.close',
                'code': 1000,
            })

    async def send_data(self, send: Callable, data: bytes) -> None:
        await send({
            'type': 'websocket.send',
            'bytes': data,
        })

    async def accept_connection(self, send: Callable) -> None:
        if not self._accepted:
            await send({
                'type': 'websocket.accept',
            })
            self._accepted = True
