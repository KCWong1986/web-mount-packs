#!/usr/bin/env python3
# coding: utf-8

from __future__ import annotations

__author__ = "ChenyangGao <https://chenyanggao.github.io>"
__version__ = (0, 0, 1)
__all__ = ["request"]

from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from gzip import decompress as decompress_gzip
from inspect import isawaitable
from json import loads
from re import compile as re_compile
from typing import Any, Literal
from zlib import compressobj, DEF_MEM_LEVEL, DEFLATED, MAX_WBITS

from asynctools import run_async
from argtools import argcount
from filewrap import bio_chunk_async_iter
from multidict import CIMultiDict
from blacksheep.client import ClientSession
from blacksheep.common.types import normalize_headers
from blacksheep.contents import Content, FormContent, JSONContent, StreamedContent, TextContent
from blacksheep.exceptions import HTTPException
from blacksheep.messages import Request, Response


CRE_search_charset = re_compile(r"\bcharset=(?P<charset>[^ ;]+)").search

if "__del__" not in ClientSession.__dict__:
    def close(self, /):
        try:
            run_async(ClientSession.close(self))
        except:
            pass
    setattr(ClientSession, "__del__", close)


def get_charset(content_type: str, default="utf-8") -> str:
    match = CRE_search_charset(content_type)
    if match is None:
        return "utf-8"
    return match["charset"]


def decompress_deflate(data: bytes, compresslevel: int = 9) -> bytes:
    # Fork from: https://stackoverflow.com/questions/1089662/python-inflate-and-deflate-implementations#answer-1089787
    compress = compressobj(
            compresslevel,  # level: 0-9
            DEFLATED,       # method: must be DEFLATED
            -MAX_WBITS,     # window size in bits:
                            #   -15..-8: negate, suppress header
                            #   8..15: normal
                            #   16..30: subtract 16, gzip header
            DEF_MEM_LEVEL,  # mem level: 1..8/9
            0               # strategy:
                            #   0 = Z_DEFAULT_STRATEGY
                            #   1 = Z_FILTERED
                            #   2 = Z_HUFFMAN_ONLY
                            #   3 = Z_RLE
                            #   4 = Z_FIXED
    )
    deflated = compress.compress(data)
    deflated += compress.flush()
    return deflated


async def decompress_response(resp: ResponseWrapper, /) -> bytes:
    data = await resp.read()
    content_encoding = resp.headers.get("Content-Encoding")
    match content_encoding:
        case "gzip":
            data = decompress_gzip(data)
        case "deflate":
            data = decompress_deflate(data)
        case "br":
            from brotli import decompress as decompress_br # type: ignore
            data = decompress_br(data)
        case "zstd":
            from zstandard import decompress as decompress_zstd
            data = decompress_zstd(data)
    return data


class ResponseWrapper:

    def __init__(self, response: Response):
        self.response = response
        self.headers = CIMultiDict((str(k, "latin-1"), str(v, "latin-1")) for k, v in response.headers)

    def __dir__(self, /) -> list[str]:
        s = set(super().__dir__())
        s.update(dir(self.response))
        return sorted(s)

    def __getattr__(self, attr, /):
        return getattr(self.response, attr)

    def __repr__(self, /):
        return f"{type(self).__qualname__}({self.response!r})"


async def request(
    url: str, 
    method: str = "GET", 
    params: None | Mapping | Iterable[tuple[Any, Any]] = None, 
    data: Any = None, 
    json: Any = None, 
    headers: None | Mapping | Iterable[tuple[Any, Any]] = None, 
    parse: Literal[None, ...] | bool | Callable = None, # type: ignore
    raise_for_status: bool = True, 
    session: None | ClientSession = None, 
):
    if session is None:
        async with ClientSession() as session:
            return await request(
                url, 
                method, 
                params=params, 
                data=data, 
                headers=headers, 
                parse=parse, 
                raise_for_status=raise_for_status, 
                session=session, 
            )
    headers = normalize_headers(headers) if headers else None
    req = Request(method.upper(), session.get_url(url, params), headers)
    if json is not None and not isinstance(json, JSONContent):
        data = JSONContent(json)
    if data:
        if isinstance(data, Content):
            content = data
        elif isinstance(data, str):
            content = TextContent(data)
        elif isinstance(data, (bytes, bytearray, memoryview)):
            content = Content(b"application/octet-stream", data)
        elif hasattr(data, "read"):
            async def gen():
                async for chunk in bio_chunk_async_iter(data):
                    yield chunk
            content = StreamedContent(b"application/octet-stream", gen)
        elif isinstance(data, AsyncIterator):
            async def gen():
                async for chunk in data:
                    yield chunk
            content = StreamedContent(b"application/octet-stream", gen)
        else:
            content = FormContent(data)
        req = req.with_content(content)
    resp = ResponseWrapper(await session.send(req))
    if resp.status >= 400:
        raise HTTPException(resp.status, resp.reason)
    if parse is None or parse is ...:
        return resp
    elif parse is False:
        return await decompress_response(resp)
    elif parse is True:
        data = await decompress_response(resp)
        content_type = resp.headers.get("Content-Type", "")
        if content_type == "application/json":
            return loads(data)
        elif content_type.startswith("application/json;"):
            return loads(data.decode(get_charset(content_type)))
        elif content_type.startswith("text/"):
            return data.decode(get_charset(content_type))
        return data
    else:
        ac = argcount(parse)
        if ac == 1:
            ret = parse(resp)
        else:
            ret = parse(resp, await decompress_response(resp))
        if isawaitable(ret):
            ret = await ret
        return ret
