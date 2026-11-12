"""Resolve public Bilibili media through normal metadata/playback APIs.

This avoids requiring the HTML video page, which may return 412 independently of
an accessible API. Never solve challenges, sign into accounts, or use preview
streams as full videos. CDN credentials remain in a temporary yt-dlp info file.
"""
from urllib.parse import urlsplit,urljoin

import httpx

from .media import allowed_asset
from .search import canonical_video_url

HEADERS={
    'User-Agent':'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Referer':'https://www.bilibili.com/',
    'Origin':'https://www.bilibili.com',
}


class SourceError(Exception):
    def __init__(self,message,retryable=True):
        super().__init__(message);self.retryable=retryable


def stream_url(raw):
    if isinstance(raw,str) and raw.startswith('http://'):raw='https://'+raw[7:]
    if not allowed_asset(raw,'stream'):
        raise SourceError('B站返回的媒体来源不受支持',False)
    return raw


def media_url(row):
    candidates=[row.get('baseUrl') or row.get('base_url') or row.get('url'),
                *(row.get('backupUrl') or row.get('backup_url') or [])]
    # Audio may prefer an overseas CDN while supplying an approved bilivideo backup.
    # Keep the CDN boundary and choose the valid backup rather than reject the video.
    for candidate in candidates:
        try:return stream_url(candidate)
        except SourceError:continue
    raise SourceError('B站未提供受支持的媒体来源',False)


def formats(play):
    output=[];dash=play.get('dash') or {}
    for kind in ('video','audio'):
        for index,row in enumerate(dash.get(kind) or []):
            if not isinstance(row,dict):continue
            try:raw=media_url(row)
            except SourceError:continue
            codec=row.get('codecs') or ''
            audio=kind=='audio'
            output.append({'format_id':f'{kind}-{row.get("id",index)}-{row.get("codecid",index)}',
                'url':stream_url(raw),'ext':'m4a' if audio else 'mp4','protocol':'https',
                'vcodec':'none' if audio else codec,'acodec':codec if audio else 'none',
                'width':row.get('width'),'height':row.get('height'),
                'tbr':float(row.get('bandwidth') or 0)/1000,
                'filesize':row.get('size'),'http_headers':{**HEADERS,'Range':'bytes=0-'}})
    if any(f['vcodec']!='none' for f in output):return output
    output=[]
    parts=play.get('durl') or []
    # Progressive multi-segment videos need explicit concatenation; prefer DASH.
    if len(parts)==1 and parts[0].get('url'):
        quality=int(play.get('quality') or 16)
        output.append({'format_id':f'progressive-{quality}','url':media_url(parts[0]),
            'ext':'mp4','protocol':'https','height':{16:360,32:480,64:720,80:1080}.get(quality),
            'filesize':parts[0].get('size'),'http_headers':{**HEADERS,'Range':'bytes=0-'}})
    return output


async def resolve(url,check_active=None):
    canonical=canonical_video_url(url)
    if not canonical or urlsplit(canonical).hostname!='www.bilibili.com':
        raise SourceError('不是受支持的 B站视频直链',False)
    identifier=canonical.rsplit('/',1)[-1]
    query={'bvid':identifier} if identifier.startswith('BV') else {'aid':identifier[2:]}
    async with httpx.AsyncClient(timeout=12,follow_redirects=False,headers=HEADERS) as client:
        async def request(endpoint,params=None,json_body=True):
            for redirect in range(4):
                if check_active and not check_active():raise SourceError('任务已取消或租约已变更',False)
                try:r=await client.get(endpoint,params=params)
                except httpx.HTTPError as exc:raise SourceError('B站连接失败，请检查网络后重试') from exc
                if r.status_code not in (301,302,303,307,308):break
                target=urljoin(str(r.url),r.headers.get('location',''))
                parsed=urlsplit(target)
                if redirect==3 or parsed.scheme!='https' or parsed.hostname not in ('www.bilibili.com','api.bilibili.com') or parsed.username or parsed.password or parsed.port not in (None,443):
                    raise SourceError('B站返回了不支持的重定向',False)
                endpoint=target;params=None
            if r.status_code in (412,429):
                raise SourceError(f'B站限制了当前请求（HTTP {r.status_code}），请稍后重试',False)
            if r.status_code in (401,403):
                raise SourceError(f'B站拒绝访问（HTTP {r.status_code}），需检查登录状态或视频权限',False)
            if r.status_code!=200:raise SourceError(f'B站接口返回 HTTP {r.status_code}')
            if not json_body:return None
            try:body=r.json()
            except ValueError as exc:raise SourceError('无法读取 B站接口响应') from exc
            if not isinstance(body,dict):raise SourceError('B站接口响应格式不正确')
            code=body.get('code')
            if code!=0:
                if code in (-101,-403,6002105):raise SourceError('B站视频需要登录或当前账号没有观看权限',False)
                if code in (-352,-412,-509):raise SourceError('B站限制了当前请求，请稍后重试',False)
                if code in (-404,62002,62012):raise SourceError('B站视频不存在、已下架或不可访问',False)
                raise SourceError('B站未提供可用视频信息',False)
            if not isinstance(body.get('data'),dict):raise SourceError('B站接口缺少视频信息')
            return body['data']
        # Establish an ordinary anonymous site session before requesting public API data.
        await request('https://www.bilibili.com/',json_body=False)
        data=await request('https://api.bilibili.com/x/web-interface/view',query)
        pages=data.get('pages') or []
        page=pages[0] if pages and isinstance(pages[0],dict) else {}
        cid=page.get('cid') or data.get('cid');aid=data.get('aid');bvid=data.get('bvid') or identifier
        duration=page.get('duration') or data.get('duration')
        if not cid or not aid or not isinstance(duration,(int,float)) or duration<=0:
            raise SourceError('B站未提供有效的分 P 信息',False)
        play=await request('https://api.bilibili.com/x/player/playurl',
                           {'bvid':bvid,'avid':aid,'cid':cid,'qn':64,'fnval':16,'fourk':0})
        if play.get('is_preview') or play.get('isPreview'):
            raise SourceError('B站只返回试看内容，不能作为完整视频缓存',False)
        actual=(play.get('timelength') or 0)/1000
        if actual<=0 or actual<duration*.95:
            raise SourceError('B站返回的视频不完整或仅包含试看内容',False)
        available=formats(play)
        if not available:
            progressive=await request('https://api.bilibili.com/x/player/playurl',
                {'bvid':bvid,'avid':aid,'cid':cid,'qn':32,'fnval':1,'platform':'html5'})
            length=(progressive.get('timelength') or 0)/1000
            if progressive.get('is_preview') or length<duration*.95:
                raise SourceError('B站只返回试看内容',False)
            available=formats(progressive)
        if not available:raise SourceError('B站未提供支持的完整视频流',False)
        title=str(data.get('title') or bvid)
        if len(pages)>1:title+=' · P1 '+str(page.get('part') or '')
        return {'id':f'{bvid}_{cid}','title':title,'description':str(data.get('desc') or ''),
            'duration':actual,'uploader':(data.get('owner') or {}).get('name') or 'Bilibili',
            'webpage_url':canonical,'extractor':'bilibili-api','extractor_key':'BiliBili',
            'formats':available,'http_headers':HEADERS,'is_live':False}
