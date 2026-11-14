"""ViVideo UI gateway. Run with PYTHONPATH=python uvicorn vivideo.app:app."""
import asyncio
import os
from pathlib import Path
from uuid import UUID, uuid4
from typing import Literal

import zmq
import zmq.asyncio
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from . import __version__
from .model_configs import router as model_configs_router
from .state_api import router as state_router
from .notes_api import router as notes_router
from .media import router as media_router
from .library_api import router as library_router
from .storage import StateError
from .service_status import router as service_status_router

STATIC = Path(__file__).parent / 'static'
app = FastAPI(title='ViVideo', version=__version__)
app.include_router(model_configs_router)
app.include_router(state_router)
app.include_router(notes_router)
app.include_router(media_router)
app.include_router(service_status_router)
app.include_router(library_router)
app.mount('/static', StaticFiles(directory=STATIC), name='static')
MODE = os.getenv('VIVIDEO_MODE', 'zmq')
ENDPOINT = os.getenv('VIVIDEO_ZMQ_ENDPOINT', 'tcp://127.0.0.1:5555')

class ChatRequest(BaseModel):
    space_id: Literal["learning", "roaming"] = "learning"
    session_id: UUID | None = None
    request_id: UUID = Field(default_factory=uuid4)
    message: str = Field(min_length=1, max_length=4000)
    history: list[dict[str, str]] = Field(default_factory=list, max_length=50)
    preferences: list[str] = Field(default_factory=list, max_length=30)
    personalized: bool = True

VIDEOS = [
    dict(id='agent', title='从零理解 AI Agent：让模型开始行动', author='开发者学习室', minutes=16, category='AI Agent', level='入门', style='概念 + 实践', color='mint', mark='AGENT', subtitle='从一次对话，到一个工作流', summary='通过工具调用、记忆和规划三个概念，梳理 Agent 的基本工作方式。'),
    dict(id='workflow', title='动手搭建你的第一个智能体工作流', author='代码实验室', minutes=19, category='工程实践', level='入门', style='代码实战', color='purple', mark='BUILD', subtitle='把想法，变成可以运行的代码', summary='从拆解需求开始，串起模型、工具与执行流程，建立最小可运行原型。'),
    dict(id='rag', title='RAG 入门：给大模型一本自己的参考书', author='知识切片', minutes=12, category='大语言模型', level='入门', style='图解讲解', color='peach', mark='RAG', subtitle='检索 × 生成，让回答有据可依', summary='用直观示例解释检索增强生成，以及它如何与 Agent 配合。'),
    dict(id='tools', title='工具调用到底是怎么工作的？', author='开发者学习室', minutes=14, category='AI Agent', level='入门', style='代码实战', color='blue', mark='TOOLS', subtitle='连接模型与真实世界', summary='了解函数描述、参数生成和执行结果回传，理解工具调用的完整链路。'),
    dict(id='design', title='一个可靠的 Agent，需要哪些设计？', author='工程笔记', minutes=18, category='工程实践', level='进阶', style='经验分享', color='yellow', mark='THINK', subtitle='可观察 · 可控制 · 可迭代', summary='从状态管理、错误处理和评估三个方面，认识 Agent 工程中的常见问题。'),
]

ROAMING_VIDEOS = [
    dict(id='roam-venice', title='在威尼斯慢慢走：水巷、石桥与傍晚的风', author='漫游手记', minutes=12, category='威尼斯', level='轻松', style='城市漫步', color='blue', mark='VENICE', subtitle='沿着水巷，走进一段慢时光', summary='沿威尼斯的小巷和石桥闲逛，听水声与远处的街头音乐，看看傍晚运河边的日常。', reason='示例理由：没有密集攻略和赶路节奏，适合放空时跟着镜头慢慢走。'),
    dict(id='roam-coast', title='海边的一个下午，只有浪声和日落', author='风景留声机', minutes=8, category='海边风景', level='轻松', style='自然白噪音', color='mint', mark='COAST', subtitle='今天，把时间交给海浪', summary='看看海浪、沙滩和落日，听一会儿自然环境声，留一段不需要做任何事的时间。', reason='示例理由：时长短、节奏舒缓，适合休息时切换心情。'),
    dict(id='roam-kyoto', title='京都小巷散步：咖啡香与路边的猫', author='走走停停', minutes=14, category='京都', level='轻松', style='生活记录', color='peach', mark='KYOTO', subtitle='在小街转角，遇见一点可爱', summary='走过安静街巷，看看小店门口的植物和街边的猫，在一杯咖啡的时间里感受城市日常。', reason='示例理由：用轻松的生活细节代替景点清单，适合随意看看。'),
    dict(id='roam-rain', title='下雨天的小咖啡馆，听雨读一会儿书', author='日常慢镜头', minutes=10, category='日常放松', level='轻松', style='氛围记录', color='purple', mark='RAIN', subtitle='窗外下雨，窗内刚好', summary='窗外的雨声、热咖啡和翻书声，组成一个安静的小角落，可以陪你休息片刻。', reason='示例理由：安静的氛围适合短暂休息，也可以作为轻柔的背景陪伴。'),
    dict(id='roam-lake', title='去山间湖边坐坐：云、树影和微风', author='山野之间', minutes=11, category='山野自然', level='轻松', style='治愈风景', color='yellow', mark='LAKE', subtitle='不赶路，就在这里停一会儿', summary='沿山间小路来到湖边，看云和树影缓慢移动，感受风吹过水面的声音。', reason='示例理由：自然风景与慢镜头相结合，适合想暂时远离喧闹的时候。'),
]

@app.get('/')
async def index():
    return FileResponse(STATIC / 'index.html')

@app.exception_handler(StateError)
async def state_error_handler(request, exc):
    return JSONResponse(status_code=exc.status,content={'detail':str(exc)})


async def worker_request(payload, timeout=180):
    context = zmq.asyncio.Context.instance()
    socket = context.socket(zmq.REQ)
    socket.setsockopt(zmq.LINGER, 0)
    socket.connect(ENDPOINT)
    try:
        await socket.send_json(payload)
        result = await asyncio.wait_for(socket.recv_json(), timeout=timeout)
        if not isinstance(result,dict):
            raise HTTPException(502,'后端响应格式不正确')
        if result.get('error'):
            status=result.get('status_code',502)
            raise HTTPException(status if status in (404,409,422,502,503,504) else 502,result['error'])
        return result
    except asyncio.TimeoutError:
        raise HTTPException(504,'后端暂未响应，请确认 Worker 已启动，稍后重试。')
    except zmq.ZMQError:
        raise HTTPException(502,'无法连接对话后端')
    finally:
        socket.close()


@app.get('/api/health')
async def health():
    connected=False
    if MODE=='zmq':
        try:
            connected=(await worker_request({'action':'ping'},timeout=1))['status']=='ok'
        except (HTTPException,KeyError):
            pass
    return {'status':'ok','mode':MODE,'transport':'zmq','backend_connected':connected}

@app.get('/api/videos')
async def videos(space_id: Literal['learning', 'roaming'] = 'learning'):
    return {'videos': ROAMING_VIDEOS if space_id == 'roaming' else VIDEOS, 'demo': True}

@app.post('/api/chat')
async def chat(request: ChatRequest):
    if not request.message.strip():
        raise HTTPException(422, '请输入推荐需求')
    if MODE == 'zmq':
        if request.session_id is None:
            raise HTTPException(422,'请先创建会话，再发送消息。')
        await worker_request({'action':'ping'},timeout=2)
        result=await worker_request({'version':1,'request_id':str(request.request_id),'action':'chat',
            'payload':{'space_id':request.space_id,'session_id':str(request.session_id),
                       'message':request.message.strip(),'personalized':request.personalized}})
        if not isinstance(result.get('message'),str) or not result['message'].strip() or not isinstance(result.get('videos'),list):
            raise HTTPException(502,'后端响应格式不正确')
        return result
    return {'message': '已收到你的需求。这里展示 5 条示例推荐，帮助你预览推荐理由、收藏和反馈流程。真实的意图理解与视频筛选将在推荐后端接入后启用。', 'videos': [{**v, 'reason': v.get('reason', '示例理由：内容围绕 AI 工程展开，包含概念解释或实践步骤，方便按学习目标选择。')} for v in (ROAMING_VIDEOS if request.space_id == 'roaming' else VIDEOS)], 'demo': True}
