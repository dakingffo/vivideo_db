"""Search adapters. Only canonical direct video URLs are accepted as candidates."""
import asyncio
import os
import re
from urllib.parse import parse_qs, urlsplit

import httpx


class SearchError(Exception):
    pass


def canonical_video_url(raw):
    if not isinstance(raw, str) or len(raw)>2000:
        return None
    try:
        url = urlsplit(raw)
        if url.scheme not in ('http', 'https') or url.username or url.password or url.port not in (None,80,443):
            return None
        host = (url.hostname or '').lower()
        if host in ('www.bilibili.com','bilibili.com'):
            match = re.fullmatch(r'/video/(BV[0-9A-Za-z]+|av[0-9]+)/?', url.path)
            if match:
                return 'https://www.bilibili.com/video/'+match[1]
        video_id = None
        if host in ('www.youtube.com','youtube.com','m.youtube.com'):
            if url.path == '/watch':
                video_id = parse_qs(url.query).get('v', [''])[0]
            elif url.path.startswith('/shorts/'):
                video_id = url.path.split('/')[2]
        elif host == 'youtu.be':
            video_id = url.path.strip('/')
        if video_id and re.fullmatch(r'[\w-]{11}', video_id, flags=re.ASCII):
            return 'https://www.youtube.com/watch?v='+video_id
        if host in ('vimeo.com','www.vimeo.com') and re.fullmatch(r'/[0-9]+/?',url.path):
            return 'https://vimeo.com/'+url.path.strip('/')
    except ValueError:
        pass
    return None


async def search_videos(query, limit=6):
    query = query.strip()
    if not query or len(query)>300:
        raise SearchError('搜索词需要在 1 到 300 字之间')
    limit = max(1,min(limit,10))
    provider = os.environ.get('VIVIDEO_SEARCH_PROVIDER','ddgs')
    search_query = query+' (site:bilibili.com/video/ OR site:youtube.com/watch OR site:vimeo.com)'
    try:
        if provider == 'tavily':
            key = os.environ.get('TAVILY_API_KEY','')
            if not key:
                raise SearchError('搜索服务缺少 TAVILY_API_KEY')
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post('https://api.tavily.com/search', headers={'Authorization': 'Bearer '+key},
                    json={'query': search_query,'max_results':limit*2,'include_answer':False,'include_raw_content':False})
                response.raise_for_status()
                rows = response.json()['results']
        elif provider == 'searxng':
            base = os.environ.get('SEARXNG_URL','').rstrip('/')
            if not base:
                raise SearchError('搜索服务缺少 SEARXNG_URL')
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(base+'/search',params={'q':search_query,'format':'json'})
                response.raise_for_status()
                rows = response.json()['results']
        elif provider == 'ddgs':
            def run():
                from ddgs import DDGS
                return list(DDGS(timeout=15).text(search_query, max_results=limit*2))
            rows = await asyncio.wait_for(asyncio.to_thread(run),timeout=22)
        else:
            raise SearchError('未知的搜索服务配置')
    except SearchError:
        raise
    except Exception as exc:
        # Never expose provider response bodies or authentication material to the model.
        raise SearchError('网络搜索暂不可用，请稍后重试；不得编造视频链接') from exc
    results, seen = [], set()
    for row in rows:
        url = canonical_video_url(row.get('url') or row.get('href'))
        if not url or url in seen:
            continue
        seen.add(url)
        results.append({'url':url,'title':str(row.get('title') or url)[:300],
                        'summary':str(row.get('content') or row.get('body') or '')[:1000],'query':query})
        if len(results)>=limit:
            break
    return results
