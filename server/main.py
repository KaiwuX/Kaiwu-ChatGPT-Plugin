import os
from typing import Optional

import uvicorn
from fastapi import (
    Body,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from loguru import logger
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from datastore.factory import get_datastore
from models.api import (
    DeleteRequest,
    DeleteResponse,
    QueryRequest,
    QueryResponse,
    UpsertRequest,
    UpsertResponse,
)
from models.models import DocumentMetadata, Source
from services.file import get_document_from_file
from services.wix_oauth import (
    get_member_access_token,
    wix_get_callback_url,
    wix_get_subscription,
)

bearer_scheme = HTTPBearer()
BEARER_TOKEN = os.environ.get("BEARER_TOKEN")
assert BEARER_TOKEN is not None


def validate_token(credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)):
    if credentials.scheme != "Bearer" or credentials.credentials != BEARER_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing token")
    return credentials


app = FastAPI(dependencies=[Depends(validate_token)])
app.mount("/.well-known", StaticFiles(directory=".well-known"), name="static")
app.mount("/static", StaticFiles(directory="static"), name="static")


oauth_app = FastAPI()
templates = Jinja2Templates(directory="templates")
oauth_app.add_middleware(SessionMiddleware, secret_key="your-secret-key")


def get_oauth_params(
    response_type: str = Query(...),
    client_id: str = Query(...),
    scope: str = Query(...),
    state: str = Query(...),
    redirect_uri: str = Query(...),
) -> dict:
    return {
        "response_type": response_type,
        "client_id": client_id,
        "scope": scope,
        "state": state,
        "redirect_uri": redirect_uri,
    }


async def get_session_data(request: Request):
    return request.session


@oauth_app.get("/login/")
async def login(
    request: Request,
    oauth_params: dict = Depends(get_oauth_params),
    session_data: dict = Depends(get_session_data),
):
    session_data.update(oauth_params)
    return templates.TemplateResponse("login.html", {"request": request})


@oauth_app.post("/login/")
async def login_post(
    username: str = Form(...),
    password: str = Form(...),
    session_data: dict = Depends(get_session_data),
):
    # response_type = session_data.get("response_type")
    # client_id = session_data.get("client_id")
    # scope = session_data.get("scope")
    state = session_data.get("state")
    # redirect_uri = session_data.get("redirect_uri")

    wix_callback_url, code_verifier = await wix_get_callback_url(
        username=username, password=password, state=state
    )

    session_data["wix_callback_url"] = wix_callback_url
    session_data["code_verifier"] = code_verifier

    # redirect to callback url
    url = f"../callback/"
    raise HTTPException(
        status_code=status.HTTP_303_SEE_OTHER, headers={"Location": url}
    )


@oauth_app.get("/callback/")
async def callback(request: Request, session_data: dict = Depends(get_session_data)):
    wix_callback_url = session_data.get("wix_callback_url")

    return templates.TemplateResponse(
        "callback.html",
        {
            "request": request,
            "wix_callback_url": wix_callback_url,
        },
    )


class SubscriptionRequest(BaseModel):
    code: str
    state: str


@oauth_app.post("/callback/")
async def subscription(
    request: SubscriptionRequest, session_data: dict = Depends(get_session_data)
):
    # response_type = session_data.get("response_type")
    # client_id = session_data.get("client_id")
    # scope = session_data.get("scope")
    state = session_data.get("state")
    redirect_uri = session_data.get("redirect_uri")
    # state from wix
    openai_code = os.environ.get("OPENAI_CODE")
    url = redirect_uri + f"?state={state}&code={openai_code}"

    wix_code = request.code

    member_access_token, member_refresh_token = await get_member_access_token(
        wix_code, session_data["code_verifier"]
    )

    subscription = await wix_get_subscription(member_access_token)

    if subscription == "Elite":
        return JSONResponse(content={"message": "You are an Elite member.", "url": url})

    else:
        return JSONResponse(
            content={
                "message": "You are not an Elite member.",
                "url": "https://www.kaiwu.info",
            }
        )


@oauth_app.post("/authorization/")
async def authorization(
    client_id: str = Form(...),
    client_secret: str = Form(...),
    code: str = Form(...),
):
    if (
        client_id != os.environ.get("CLIENT_ID")
        or client_secret != os.environ.get("CLIENT_SECRET")
        or code != os.environ.get("OPENAI_CODE")
    ):
        raise HTTPException(status_code=401, detail="Invalid or missing token")

    return {"access_token": os.environ.get("BEARER_TOKEN"), "token_type": "bearer"}


app.mount("/oauth", oauth_app)

# Create a sub-application, in order to access just the query endpoint in an OpenAPI schema, found at http://0.0.0.0:8000/sub/openapi.json when the app is running locally
sub_app = FastAPI(
    title="Retrieval Plugin API",
    description="A retrieval API for querying and filtering documents based on natural language queries and metadata",
    version="1.0.0",
    servers=[{"url": "https://your-app-url.com"}],
    dependencies=[Depends(validate_token)],
)
app.mount("/sub", sub_app)


@app.post(
    "/upsert-file",
    response_model=UpsertResponse,
)
async def upsert_file(
    file: UploadFile = File(...),
    metadata: Optional[str] = Form(None),
):
    try:
        metadata_obj = (
            DocumentMetadata.parse_raw(metadata)
            if metadata
            else DocumentMetadata(source=Source.file)
        )
    except:
        metadata_obj = DocumentMetadata(source=Source.file)

    document = await get_document_from_file(file, metadata_obj)

    try:
        ids = await datastore.upsert([document])
        return UpsertResponse(ids=ids)
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail=f"str({e})")


@app.post(
    "/upsert",
    response_model=UpsertResponse,
)
async def upsert(
    request: UpsertRequest = Body(...),
):
    try:
        ids = await datastore.upsert(request.documents)
        return UpsertResponse(ids=ids)
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail="Internal Service Error")


@app.post(
    "/query",
    response_model=QueryResponse,
)
async def query_main(
    request: QueryRequest = Body(...),
):
    try:
        results = await datastore.query(
            request.queries,
        )
        return QueryResponse(results=results)
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail="Internal Service Error")


@sub_app.post(
    "/query",
    response_model=QueryResponse,
    # NOTE: We are describing the shape of the API endpoint input due to a current limitation in parsing arrays of objects from OpenAPI schemas. This will not be necessary in the future.
    description="Accepts search query objects array each with query and optional filter. Break down complex questions into sub-questions. Refine results by criteria, e.g. time / source, don't do this often. Split queries if ResponseTooLargeError occurs.",
)
async def query(
    request: QueryRequest = Body(...),
):
    try:
        results = await datastore.query(
            request.queries,
        )
        return QueryResponse(results=results)
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail="Internal Service Error")


@app.delete(
    "/delete",
    response_model=DeleteResponse,
)
async def delete(
    request: DeleteRequest = Body(...),
):
    if not (request.ids or request.filter or request.delete_all):
        raise HTTPException(
            status_code=400,
            detail="One of ids, filter, or delete_all is required",
        )
    try:
        success = await datastore.delete(
            ids=request.ids,
            filter=request.filter,
            delete_all=request.delete_all,
        )
        return DeleteResponse(success=success)
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=500, detail="Internal Service Error")


@app.on_event("startup")
async def startup():
    global datastore
    datastore = await get_datastore()


def start():
    uvicorn.run("server.main:app", host="0.0.0.0", port=8000, reload=True)
