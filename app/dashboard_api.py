"""Same-origin browser API. All data access derives identity from an HttpOnly session."""
from datetime import date
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.corrections import Correction
from app.dashboard_store import SESSION_SECONDS
from app.pipeline import DuplicateReceiptError
from app.review import today
from app.storage import StorageError

COOKIE = '__Host-receipt_session'
STATIC = Path(__file__).parent / 'static'


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Login(Input):
    token: str = Field(min_length=43, max_length=43)


class Edit(Input):
    field: Literal['date', 'total', 'vendor', 'category']
    value: str = Field(min_length=1, max_length=200)


class Review(Input):
    action: Literal['confirm', 'category', 'retry']
    value: str = Field(default='', max_length=100)


class Alias(Input):
    action: Literal['add', 'remove']
    name: str = Field(min_length=1, max_length=200)
    target: str = Field(default='', max_length=200)


def services(request: Request):
    svc = request.app.state.services
    if svc is None or not svc.dashboard_url:
        raise HTTPException(503, 'Dashboard is not configured. Continue using Telegram.')
    return svc


def identity(request: Request, svc=Depends(services)):
    try:
        return svc.store.dashboard.authenticate(request.cookies.get(COOKIE))
    except LookupError as error:
        raise HTTPException(401, str(error)) from None


def same_origin(request: Request, svc=Depends(services)):
    if request.headers.get('origin') != svc.dashboard_url:
        raise HTTPException(403, 'This action must come from your dashboard.')


def install_dashboard(app):
    @app.middleware('http')
    async def dashboard_headers(request, call_next):
        protected = request.url.path.startswith(('/api/dashboard', '/dashboard'))
        if not protected:
            return await call_next(request)
        try:
            if request.method == 'POST' and len(await request.body()) > 16384:
                response = JSONResponse({'detail': 'Request too large.'}, status_code=413)
            else:
                response = await call_next(request)
        except DuplicateReceiptError:
            response = JSONResponse({'detail': 'This change would duplicate another receipt.'}, status_code=409)
        except LookupError as error:
            response = JSONResponse({'detail': str(error)}, status_code=404)
        except ValueError as error:
            response = JSONResponse({'detail': str(error)}, status_code=400)
        except StorageError:
            response = JSONResponse({'detail': 'The receipt image is temporarily unavailable. Try again.'}, status_code=503)
        except Exception:
            response = JSONResponse({'detail': 'The dashboard could not complete this request. Please try again.'}, status_code=500)
        response.headers.update({
            'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
            'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        })
        return response

    @app.get('/dashboard', include_in_schema=False)
    @app.get('/dashboard/', include_in_schema=False)
    def shell():
        return FileResponse(STATIC / 'dashboard.html')

    @app.get('/dashboard/assets/{name}', include_in_schema=False)
    def asset(name: str):
        if name not in {'dashboard.css', 'dashboard.js'}:
            raise HTTPException(404)
        return FileResponse(STATIC / name)

    router = APIRouter(prefix='/api/dashboard')

    @router.post('/login', dependencies=[Depends(same_origin)])
    def login(body: Login, response: Response, svc=Depends(services)):
        try:
            token = svc.store.dashboard.exchange_login(body.token)
        except LookupError as error:
            raise HTTPException(401, str(error)) from None
        response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True, secure=True, samesite='strict', path='/')
        return {'ok': True}

    @router.get('/session')
    def session(user=Depends(identity)):
        return {'month': today().strftime('%Y-%m'), 'session_hours': 24}

    @router.post('/logout', dependencies=[Depends(same_origin)])
    def logout(request: Request, response: Response, user=Depends(identity), svc=Depends(services)):
        svc.store.dashboard.logout(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE, path='/', secure=True, httponly=True, samesite='strict')
        return {'ok': True}

    @router.get('/summary')
    def summary(month: str, user=Depends(identity), svc=Depends(services)):
        result = svc.store.expenses.get_monthly_summary(user, month)
        categories = svc.store.expenses.get_category_summary(user, month)
        return {'count': result.count, 'total': str(result.total),
                'average': format(result.total / result.count, '.2f') if result.count else '0.00',
                'categories': [{'name': name or 'Unassigned', 'count': item.count, 'total': str(item.total)} for name, item in categories],
                'pending': len(svc.store.dashboard.pending(user))}

    @router.get('/receipts')
    def receipts(month: str | None = None, vendor: str | None = None, status: str | None = None,
                 page: int = 1, category: str | None = None, user=Depends(identity), svc=Depends(services)):
        return svc.store.dashboard.receipts(user, month, vendor, status, page, category=category)

    @router.get('/receipts/{receipt_id}')
    def receipt(receipt_id: UUID, user=Depends(identity), svc=Depends(services)):
        return svc.store.dashboard.receipt(user, receipt_id)

    @router.get('/receipts/{receipt_id}/image')
    async def image(receipt_id: UUID, user=Depends(identity), svc=Depends(services)):
        path = svc.store.dashboard.receipt(user, receipt_id, image=True)
        if svc.storage is None:
            raise HTTPException(503, 'Images are temporarily unavailable.')
        return {'url': await svc.storage.create_signed_url(object_path=path, expires_in_seconds=900)}

    @router.get('/receipts/{receipt_id}/history')
    def history(receipt_id: UUID, page: int = 1, user=Depends(identity), svc=Depends(services)):
        return {'text': svc.store.receipt_history(user, receipt_id, page)}

    @router.post('/receipts/{receipt_id}/edit', dependencies=[Depends(same_origin)])
    def edit(receipt_id: UUID, body: Edit, user=Depends(identity), svc=Depends(services)):
        svc.store.correct_receipt(user, Correction(receipt_id, body.field, body.value))
        return svc.store.dashboard.receipt(user, receipt_id)

    @router.get('/review')
    def reviews(user=Depends(identity), svc=Depends(services)):
        return {'items': svc.store.dashboard.pending(user)}

    @router.post('/receipts/{receipt_id}/review', dependencies=[Depends(same_origin)])
    def review(receipt_id: UUID, body: Review, user=Depends(identity), svc=Depends(services)):
        if body.action == 'confirm':
            override = date.fromisoformat(body.value) if body.value else None
            svc.store.confirm_review(user, receipt_id, override)
        elif body.action == 'category':
            from app.corrections import validated_value
            category = validated_value('category', body.value)
            svc.store.resolve_category(user, receipt_id, category)
        else:
            svc.store.retry_review(user, receipt_id)
        return {'ok': True}

    @router.get('/vendors')
    def vendors(page: int = 1, user=Depends(identity), svc=Depends(services)):
        return svc.store.dashboard.vendors(user, page)

    @router.post('/aliases', dependencies=[Depends(same_origin)])
    def alias(body: Alias, user=Depends(identity), svc=Depends(services)):
        if body.action == 'add':
            svc.store.vendors.add(user, body.name, body.target)
        else:
            svc.store.vendors.remove(user, body.name)
        return {'ok': True}

    @router.get('/export')
    def export(period: str, user=Depends(identity), svc=Depends(services)):
        result = svc.store.export_receipts(user, period)
        return Response(result.content, media_type='text/csv', headers={'Content-Disposition': f'attachment; filename="{result.filename}"'})

    app.include_router(router)
