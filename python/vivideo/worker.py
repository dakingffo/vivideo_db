"""Concurrent ZMQ ROUTER worker; compatible with one REQ socket per gateway request."""
import asyncio
import json
import logging
import os
import signal
from uuid import UUID

import zmq
import zmq.asyncio

from .agent import AgentError, process_turn
from .storage import SPACES, StateError

logger=logging.getLogger(__name__)


async def handle(payload):
    try:
        if payload.get('action')=='ping':
            return {'status':'ok'}
        if payload.get('action')!='chat':
            raise StateError('未知操作',422)
        request=payload['payload']
        space=request['space_id']
        if space not in SPACES:
            raise StateError('空间不存在',422)
        sid=str(UUID(request['session_id']))
        rid=str(UUID(payload['request_id']))
        message=request['message']
        if not isinstance(message,str) or not message.strip() or len(message)>4000:
            raise StateError('消息内容无效',422)
        return await process_turn(space,sid,rid,message.strip(),bool(request.get('personalized',True)))
    except (StateError,AgentError) as exc:
        return {'error':str(exc),'status_code':exc.status}
    except (KeyError,ValueError,TypeError):
        return {'error':'请求格式不正确','status_code':422}
    except TimeoutError:
        return {'error':'本轮处理超时，请稍后重试。','status_code':504}
    except Exception:
        # No request payloads, credentials or provider response bodies in logs.
        logger.error('Turn failed unexpectedly; request content omitted')
        return {'error':'后端处理失败，请稍后重试。','status_code':502}


async def serve():
    context=zmq.asyncio.Context()
    socket=context.socket(zmq.ROUTER)
    socket.setsockopt(zmq.LINGER,0)
    endpoint=os.environ.get('VIVIDEO_ZMQ_ENDPOINT','tcp://127.0.0.1:5555')
    socket.bind(endpoint)
    tasks=set()
    semaphore=asyncio.Semaphore(int(os.environ.get('VIVIDEO_WORKER_CONCURRENCY','8')))
    send_lock=asyncio.Lock()

    async def dispatch(frames):
        envelope,body=frames[:-1],frames[-1]
        try:
            payload=json.loads(body)
            if not isinstance(payload,dict):
                raise ValueError()
            if payload.get('action')=='ping':
                result=await handle(payload)
            else:
                try:
                    await asyncio.wait_for(semaphore.acquire(),timeout=3)
                except TimeoutError:
                    result={'error':'对话服务繁忙，请稍后重试。','status_code':503}
                else:
                    try:
                        result=await handle(payload)
                    finally:
                        semaphore.release()
        except (ValueError,TypeError):
            result={'error':'请求格式不正确','status_code':422}
        async with send_lock:
            await socket.send_multipart([*envelope,json.dumps(result,ensure_ascii=False).encode()])

    logger.info('ViVideo worker listening at %s',endpoint)
    try:
        while True:
            frames=await socket.recv_multipart()
            task=asyncio.create_task(dispatch(frames))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        socket.close()
        context.term()


async def main():
    task=asyncio.create_task(serve())
    loop=asyncio.get_running_loop()
    for sig in (signal.SIGTERM,signal.SIGINT):
        loop.add_signal_handler(sig,task.cancel)
    try:
        await task
    except asyncio.CancelledError:
        pass


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
