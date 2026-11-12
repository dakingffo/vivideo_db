"""Platform metadata, public progressive media proxy, and official embed descriptors."""
import asyncio
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from .search import canonical_video_url

router=APIRouter(prefix='/api/media')
HEADERS={'User-Agent':'Mozilla/5.0','Referer':'https://www.bilibili.com/','Accept-Encoding':'identity'}
IMAGE_HOSTS=('hdslb.com','ytimg.com','vimeocdn.com')
STREAM_HOSTS=('bilivideo.com','bilivideo.cn')
CACHE_SECONDS=6*3600


@dataclass
class Asset:
    url: str
    expires: float
    kind: str


assets: OrderedDict[str,Asset]=OrderedDict()
metadata_cache: OrderedDict[str,tuple[float,dict]]=OrderedDict()
metadata_tasks: dict[str,asyncio.Task]={}


def allowed_asset(url,kind):
    try:
        value=urlsplit(url)
        hosts=IMAGE_HOSTS if kind=='thumbnail' else STREAM_HOSTS
        host=value.hostname or ''
        return value.scheme=='https' and not value.username and not value.password and value.port in (None,443) and any(host==base or host.endswith('.'+base) for base in hosts)
    except (TypeError,ValueError):
        return False


def asset_url(url,kind,ttl=CACHE_SECONDS):
    if isinstance(url,str) and url.startswith('//'):
        url='https:'+url
    if isinstance(url,str) and url.startswith('http://'):
        url='https://'+url[7:]
    if not allowed_asset(url,kind):
        return None
    token=str(uuid4())
    assets[token]=Asset(url,time.monotonic()+ttl,kind)
    while len(assets)>2048:
        assets.popitem(last=False)
    return f'/api/media/{kind}s/{token}'


def descriptor(url):
    canonical=canonical_video_url(url)
    if canonical is None:
        raise HTTPException(422,'不支持的视频链接')
    parsed=urlsplit(canonical)
    if 'bilibili.com' in parsed.hostname:
        vid=parsed.path.split('/')[-1]
        query={'bvid':vid} if vid.startswith('BV') else {'aid':vid[2:]}
        return {'provider':'bilibili','platform_id':vid,'source_url':canonical,
                'embed_url':'https://player.bilibili.com/player.html?'+urlencode({**query,'autoplay':0,'poster':1,'danmaku':0}),
                'thumbnail_url':None,'duration_seconds':None,'tracking_supported':False}
    if parsed.hostname=='www.youtube.com':
        vid=parse_qs(parsed.query)['v'][0]
        return {'provider':'youtube','platform_id':vid,'source_url':canonical,
                'embed_url':f'https://www.youtube.com/embed/{vid}?enablejsapi=1&playsinline=1',
                'thumbnail_url':asset_url(f'https://i.ytimg.com/vi/{vid}/hqdefault.jpg','thumbnail'),
                'duration_seconds':None,'tracking_supported':True}
    vid=parsed.path.strip('/')
    return {'provider':'vimeo','platform_id':vid,'source_url':canonical,
            'embed_url':f'https://player.vimeo.com/video/{vid}?playsinline=1',
            'thumbnail_url':None,'duration_seconds':None,'tracking_supported':True}


class OpenGraphParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.image=None

    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='meta' and (attrs.get('property') or attrs.get('name')) in ('og:image','twitter:image'):
            self.image=attrs.get('content')


def positive_duration(value):
    try:
        value=float(value)
        return value if 0<value<604800 else None
    except (ValueError,TypeError):
        return None


async def fetch_metadata(url):
    result=descriptor(url)
    async with httpx.AsyncClient(timeout=6,headers=HEADERS) as client:
        if result['provider']=='bilibili':
            vid=result['platform_id']
            params={'bvid':vid} if vid.startswith('BV') else {'aid':vid[2:]}
            try:
                response=await client.get('https://api.bilibili.com/x/web-interface/view',params=params)
                response.raise_for_status()
                data=response.json()
                if data.get('code')!=0:
                    raise ValueError('unavailable')
                info=data['data']
                result['thumbnail_url']=asset_url(info.get('pic',''),'thumbnail')
                pages=info.get('pages') or []
                result['duration_seconds']=positive_duration(pages[0].get('duration') if pages else info.get('duration'))
                result['_cid']=info.get('cid')
                result['_aid']=info.get('aid')
            except (httpx.HTTPError,ValueError,KeyError,TypeError):
                # A blocked API or private video is not bypassed; try public page metadata only.
                try:
                    response=await client.get(result['source_url'])
                    response.raise_for_status()
                    parser=OpenGraphParser();parser.feed(response.text[:1_000_000])
                    if parser.image:
                        result['thumbnail_url']=asset_url(parser.image,'thumbnail')
                except (httpx.HTTPError,ValueError):
                    pass
        elif result['provider']=='vimeo':
            try:
                response=await client.get('https://vimeo.com/api/oembed.json',params={'url':result['source_url'],'maxwidth':640})
                response.raise_for_status();data=response.json()
                result['thumbnail_url']=asset_url(data.get('thumbnail_url',''),'thumbnail')
                result['duration_seconds']=positive_duration(data.get('duration'))
            except (httpx.HTTPError,ValueError,KeyError,TypeError):
                pass
    return result


async def metadata(url):
    canonical=canonical_video_url(url)
    if canonical is None:
        raise HTTPException(422,'不支持的视频链接')
    cached=metadata_cache.get(canonical)
    if cached and cached[0]>time.monotonic():
        poster=cached[1].get('thumbnail_url')
        asset=assets.get(poster.rsplit('/',1)[-1]) if poster else None
        if not poster or asset and asset.expires>time.monotonic():
            metadata_cache.move_to_end(canonical)
            return dict(cached[1])
    task=metadata_tasks.get(canonical)
    if task is None:
        task=asyncio.create_task(fetch_metadata(canonical))
        metadata_tasks[canonical]=task
    try:
        result=await asyncio.shield(task)
        # Retry missing cover metadata sooner; successful public metadata lasts six hours.
        ttl=CACHE_SECONDS if result.get('thumbnail_url') else 60
        metadata_cache[canonical]=(time.monotonic()+ttl,result)
        while len(metadata_cache)>512:
            metadata_cache.popitem(last=False)
        return dict(result)
    finally:
        if task.done() and metadata_tasks.get(canonical) is task:
            metadata_tasks.pop(canonical,None)


def public_metadata(info):
    return {key:value for key,value in info.items() if not key.startswith('_')}


async def playback(url):
    info=await metadata(url)
    result={**public_metadata(info),'kind':info['provider']}
    if info['provider']!='bilibili' or not info.get('_cid'):
        return result
    params={'cid':info['_cid'],'qn':16,'fnval':1,'platform':'html5'}
    vid=info['platform_id']
    params['bvid' if vid.startswith('BV') else 'avid']=vid if vid.startswith('BV') else vid[2:]
    try:
        async with httpx.AsyncClient(timeout=8,headers=HEADERS) as client:
            response=await client.get('https://api.bilibili.com/x/player/playurl',params=params)
            response.raise_for_status();data=response.json()
            if data.get('code')!=0:
                return result
            streams=data['data'].get('durl') or []
            if len(streams)!=1:
                return result
            stream=streams[0]
            # Only a complete public progressive MP4 is proxied, not DASH/FLV or preview clips.
            if not re.search(r'\.mp4(?:\?|$)',stream.get('url',''),re.I):
                return result
            stream_duration=positive_duration(data['data'].get('timelength',0)/1000)
            if info.get('duration_seconds') and (not stream_duration or stream_duration<info['duration_seconds']*.95):
                return result
            stream_url=asset_url(stream['url'],'stream',ttl=1800)
            if stream_url:
                result.update(kind='native',stream_url=stream_url,tracking_supported=True)
    except (httpx.HTTPError,ValueError,KeyError,TypeError):
        pass
    return result


async def open_asset(token,kind,headers=None,method='GET'):
    asset=assets.get(token)
    if asset is None or asset.kind!=kind or asset.expires<time.monotonic():
        raise HTTPException(404,'媒体地址已过期，请重新打开视频')
    client=httpx.AsyncClient(timeout=httpx.Timeout(25,connect=8),headers=HEADERS)
    target=asset.url
    try:
        for _ in range(4):
            if not allowed_asset(target,kind):
                raise HTTPException(502,'媒体来源地址不受支持')
            upstream=await client.send(client.build_request(method,target,headers=headers),stream=True)
            if upstream.status_code in (301,302,303,307,308):
                redirect=upstream.headers.get('location','')
                target=str(upstream.url.join(redirect))
                await upstream.aclose()
                continue
            if upstream.status_code not in (200,206,416):
                await upstream.aclose()
                raise HTTPException(502,'平台暂时无法提供此媒体，请使用平台播放器')
            return client,upstream
        raise HTTPException(502,'媒体重定向过多')
    except (httpx.HTTPError,HTTPException) as exc:
        await client.aclose()
        if isinstance(exc,HTTPException):
            raise
        raise HTTPException(502,'媒体加载失败，请稍后重试') from None


@router.get('/thumbnails/{token}')
async def thumbnail(token: str):
    client,upstream=await open_asset(token,'thumbnail')
    try:
        mime=upstream.headers.get('content-type','').split(';')[0].lower()
        if mime not in ('image/jpeg','image/png','image/webp','image/gif','image/avif') or upstream.status_code!=200:
            raise HTTPException(502,'无法读取视频封面')
        chunks=[];size=0
        async for chunk in upstream.aiter_bytes():
            size+=len(chunk)
            if size>5*1024*1024:
                raise HTTPException(502,'视频封面过大')
            chunks.append(chunk)
        return Response(b''.join(chunks),media_type=mime,headers={'Cache-Control':'public,max-age=3600','X-Content-Type-Options':'nosniff'})
    finally:
        await upstream.aclose();await client.aclose()


@router.api_route('/streams/{token}',methods=['GET','HEAD'])
async def stream(token: str,request: Request):
    byte_range=request.headers.get('range')
    if byte_range and not re.fullmatch(r'bytes=(?:\d+-\d*|-\d+)',byte_range):
        raise HTTPException(416,'不支持的 Range 请求')
    client,upstream=await open_asset(token,'stream',{'Range':byte_range} if byte_range else None,request.method)
    headers={key:value for key,value in upstream.headers.items() if key in ('content-length','content-range','accept-ranges')}
    headers.update({'Cache-Control':'private,no-store','X-Content-Type-Options':'nosniff'})
    mime=upstream.headers.get('content-type','video/mp4').split(';')[0]
    if upstream.status_code!=416 and mime not in ('video/mp4','application/octet-stream'):
        await upstream.aclose();await client.aclose()
        raise HTTPException(502,'平台未提供可播放的 MP4')

    async def close():
        await upstream.aclose();await client.aclose()

    async def chunks():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await close()

    if request.method=='HEAD':
        await close()
        return Response(status_code=upstream.status_code,headers=headers,media_type=mime)
    return StreamingResponse(chunks(),status_code=upstream.status_code,headers=headers,
                             media_type='video/mp4',background=BackgroundTask(close))
