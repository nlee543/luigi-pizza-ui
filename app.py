"""Server-rendered pizza ordering UI. API credentials never enter browser storage."""
import os
import secrets
from datetime import timedelta
from decimal import Decimal
from functools import wraps
from pathlib import Path

import requests
from cachelib.file import FileSystemCache
from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, session, url_for
from flask_session import Session

STATUSES = {
    'order_received': 'Order received', 'preparing': 'Preparing',
    'out_for_delivery': 'Out for delivery', 'ready_for_pickup': 'Ready for pickup',
    'completed': 'Completed',
}


class APIError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    Path(app.instance_path).mkdir(exist_ok=True)
    app.config.update(
        SECRET_KEY=os.environ.get('SECRET_KEY') or secrets.token_hex(32),
        API_BASE_URL=os.environ.get('API_BASE_URL', 'http://127.0.0.1:8000').rstrip('/'),
        API_CSRF_PROTECTION=os.environ.get('API_CSRF_PROTECTION', '1'),
        SESSION_TYPE='cachelib',
        SESSION_CACHELIB=FileSystemCache(str(Path(app.instance_path) / 'sessions'), threshold=1000, mode=0o600),
        SESSION_COOKIE_NAME='luigi_ui', SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE') == '1',
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        MAX_CONTENT_LENGTH=64 * 1024,
    )
    if test_config:
        app.config.update(test_config)
    Session(app)

    def api(method, path, data=None, params=None):
        headers = {'Accept': 'application/json', 'X-CSRF-Protection': app.config['API_CSRF_PROTECTION']}
        if session.get('api_csrf'):
            headers['X-CSRF-Token'] = session['api_csrf']
        try:
            with requests.Session() as client:
                client.cookies.update(session.get('api_cookies', {}))
                response = client.request(method, app.config['API_BASE_URL'] + path,
                                          json=data, params=params, headers=headers,
                                          timeout=(3, 10), allow_redirects=False)
                session['api_cookies'] = client.cookies.get_dict()
        except requests.RequestException as exc:
            raise APIError('The pizza kitchen is temporarily unavailable. Please try again.') from exc
        if response.status_code >= 300:
            try:
                detail = response.json().get('detail', 'The request could not be completed.')
                if isinstance(detail, list):
                    detail = '; '.join(f"{'.'.join(str(p) for p in e.get('loc', [])[1:])}: {e.get('msg', 'Invalid value')}" for e in detail)
                if not isinstance(detail, str):
                    detail = 'The request could not be completed.'
            except (ValueError, AttributeError):
                detail = 'The request could not be completed.'
            if response.status_code >= 500:
                detail = 'The pizza kitchen is temporarily unavailable. Please try again.'
            raise APIError(detail, response.status_code)
        if response.status_code == 204:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise APIError('The kitchen returned an unreadable response. Please try again.') from exc

    def all_items(path):
        result = []
        while True:
            batch = api('GET', path, params={'offset': len(result), 'limit': 100})
            result.extend(batch)
            if len(batch) < 100:
                return result

    def menu_data():
        if not hasattr(g, 'menu'):
            g.menu = {key: all_items('/' + key) for key in ('pizzas', 'toppings', 'drinks')}
            g.menu['pizzas'].sort(key=lambda p: Decimal(p['price']))
        return g.menu

    def login_required(staff=False):
        def decorator(view):
            @wraps(view)
            def wrapped(*args, **kwargs):
                if not session.get('user'):
                    if request.path.endswith('/poll'):
                        return jsonify(error='Please sign in again.'), 401
                    return redirect(url_for('login', staff='1' if staff else None))
                auth = api('GET', '/auth/me')
                session['user'], session['api_csrf'] = auth['user'], auth['csrf_token']
                if not auth['user']['active']:
                    session.clear()
                    abort(403)
                if staff and auth['user']['role'] not in ('staff', 'admin'):
                    abort(403)
                if auth['user']['must_change_password'] and request.endpoint not in ('password', 'logout'):
                    if request.path.endswith('/poll'):
                        return jsonify(error='Please change your password to continue.'), 403
                    return redirect(url_for('password'))
                return view(*args, **kwargs)
            return wrapped
        return decorator

    @app.before_request
    def protect_forms():
        if 'csrf' not in session:
            session['csrf'] = secrets.token_urlsafe(32)
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            token = request.form.get('csrf_token', '')
            if not secrets.compare_digest(token, session['csrf']):
                abort(400, description='Your form expired. Reload the page and try again.')

    @app.after_request
    def secure_response(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
        if request.endpoint != 'static':
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.context_processor
    def common():
        return dict(user=session.get('user'), csrf_token=session.get('csrf'), statuses=STATUSES,
                    cart_count=sum(line['quantity'] for line in session.get('cart', [])))

    @app.template_filter('money')
    def money(value):
        return f'${Decimal(str(value)):.2f}'

    @app.errorhandler(APIError)
    def api_error(error):
        if error.status == 401:
            cart = session.get('cart', [])
            session.clear()
            session['cart'] = cart
            if not request.path.endswith('/poll'):
                flash('Please sign in to continue.', 'error')
                return redirect(url_for('login'))
        if request.path.endswith('/poll'):
            return jsonify(error=str(error)), error.status
        return render_template('error.html', message=str(error)), error.status

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(413)
    def page_error(error):
        return render_template('error.html', message=error.description), error.code

    @app.route('/')
    def menu():
        try:
            return render_template('menu.html', **menu_data())
        except APIError as error:
            return render_template('menu.html', pizzas=[], drinks=[], toppings=[], menu_error=str(error)), 503

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            try:
                auth = api('POST', '/auth/login', {key: request.form.get(key, '') for key in ('email', 'password')})
                cookies, cart = session.get('api_cookies', {}), session.get('cart', [])
                session.clear()
                session.update(user=auth['user'], api_csrf=auth['csrf_token'], api_cookies=cookies,
                               csrf=secrets.token_urlsafe(32), cart=cart)
                app.session_interface.regenerate(session)
                if auth['user']['must_change_password']:
                    return redirect(url_for('password'))
                return redirect(url_for('staff' if auth['user']['role'] in ('staff', 'admin') else ('cart' if cart else 'menu')))
            except APIError as error:
                flash(str(error), 'error')
        return render_template('auth.html', mode='login')

    @app.route('/register', methods=['GET', 'POST'])
    def register():
        if request.method == 'POST':
            fields = ('email', 'name', 'address', 'phone_number', 'dine_in_name', 'password')
            try:
                api('POST', '/auth/register', {key: request.form.get(key, '') for key in fields})
                flash('Your account is ready. Sign in to place your first order.', 'success')
                return redirect(url_for('login'))
            except APIError as error:
                flash(str(error), 'error')
        return render_template('auth.html', mode='register')

    @app.post('/logout')
    def logout():
        try:
            if session.get('user'):
                api('POST', '/auth/logout')
        finally:
            session.clear()
        return redirect(url_for('menu'))

    @app.route('/account/password', methods=['GET', 'POST'])
    @login_required()
    def password():
        if request.method == 'POST':
            if request.form.get('new_password') != request.form.get('confirm_password'):
                flash('The new passwords do not match.', 'error')
            else:
                try:
                    api('POST', '/auth/change-password', {key: request.form.get(key, '') for key in ('current_password', 'new_password')})
                    session.clear()
                    flash('Password changed. Please sign in again.', 'success')
                    return redirect(url_for('login'))
                except APIError as error:
                    flash(str(error), 'error')
        return render_template('auth.html', mode='password')

    def quantity(value, allow_zero=False):
        try:
            number = int(value)
        except (ValueError, TypeError):
            abort(400, description='Enter a valid quantity.')
        if not (0 if allow_zero else 1) <= number <= 99:
            abort(400, description='Quantity must be between 1 and 99 (or 0 to remove).')
        return number

    @app.post('/cart/add')
    def add_cart():
        kind = request.form.get('kind')
        if kind not in ('pizzas', 'drinks'):
            abort(400)
        items = menu_data()
        item = next((p for p in items[kind] if str(p['id']) == request.form.get('item_id')), None)
        if item is None:
            abort(400, description='That item is no longer available.')
        selected = request.form.getlist('topping_ids') if kind == 'pizzas' else []
        if not set(selected) <= {str(t['id']) for t in items['toppings']}:
            abort(400, description='Choose toppings from the menu.')
        lines = session.get('cart', [])
        if len(lines) >= 50:
            abort(400, description='Your basket can contain up to 50 different items.')
        lines.append({'kind': kind, 'id': item['id'], 'quantity': quantity(request.form.get('quantity', '1')),
                      'topping_ids': sorted(set(map(int, selected)))})
        session['cart'] = lines
        session.pop('checkout_token', None)
        flash('Added to your basket.', 'success')
        return redirect(url_for('menu', _anchor='order-menu'))

    def basket():
        menu = menu_data()
        lines, total = [], Decimal('0')
        for line in session.get('cart', []):
            item = next((i for i in menu[line['kind']] if i['id'] == line['id']), None)
            valid_toppings = {t['id'] for t in menu['toppings']}
            available = item is not None and set(line['topping_ids']) <= valid_toppings
            price = Decimal(item['price']) if item else Decimal('0')
            title = (item['size'].title() + ' pizza' if line['kind'] == 'pizzas' else item['name'] + ' · ' + item['size']) if item else 'Unavailable item'
            lines.append(dict(line, title=title, available=available, subtotal=price * line['quantity'],
                              toppings=', '.join(t['name'] for t in menu['toppings'] if t['id'] in line['topping_ids'])))
            total += price * line['quantity']
        return lines, total

    @app.route('/cart')
    def cart():
        lines, total = basket()
        session.setdefault('checkout_token', secrets.token_urlsafe(32))
        return render_template('cart.html', lines=lines, total=total, checkout_token=session['checkout_token'])

    @app.post('/cart/update')
    def update_cart():
        lines = session.get('cart', [])
        try:
            index = int(request.form['index'])
            if index < 0 or index >= len(lines):
                abort(400)
        except (KeyError, ValueError):
            abort(400)
        count = quantity(request.form.get('quantity'), allow_zero=True)
        if count:
            lines[index]['quantity'] = count
        else:
            lines.pop(index)
        session['cart'] = lines
        session.pop('checkout_token', None)
        return redirect(url_for('cart'))

    @app.post('/checkout')
    @login_required()
    def checkout():
        token = session.get('checkout_token')
        if not token or not secrets.compare_digest(token, request.form.get('checkout_token', '')):
            abort(400, description='This basket changed or was already submitted. Review your basket again.')
        lines, _ = basket()
        if not lines or not all(line['available'] for line in lines):
            abort(400, description='Please remove unavailable items and add at least one item.')
        payload = {'pizzas': [], 'drinks': []}
        for line in lines:
            key = 'pizza_id' if line['kind'] == 'pizzas' else 'drink_id'
            entry = {key: line['id'], 'quantity': line['quantity']}
            if line['kind'] == 'pizzas':
                entry['topping_ids'] = line['topping_ids']
            payload[line['kind']].append(entry)
        is_staff = session['user']['role'] in ('staff', 'admin')
        if is_staff:
            try:
                payload['user_id'] = int(request.form.get('user_id', ''))
                if payload['user_id'] <= 0:
                    raise ValueError
            except ValueError:
                abort(400, description='Select a customer from the staff customer search.')
        try:
            order = api('POST', '/staff/orders' if is_staff else '/orders', payload)
        except APIError as error:
            if error.status >= 500:
                raise APIError('We could not confirm whether the kitchen received your order. Check your order history before submitting again to avoid a duplicate order.', error.status) from error
            raise
        session['cart'] = []
        session.pop('checkout_token', None)
        flash('Your order has been sent to the kitchen.', 'success')
        return redirect(url_for('order_detail', order_id=order['id']))

    @app.get('/orders')
    @login_required()
    def orders():
        offset = max(0, request.args.get('offset', 0, type=int))
        result = api('GET', '/orders', params={'offset': offset, 'limit': 25})
        return render_template('orders.html', orders=result, offset=offset, staff_view=False)

    @app.get('/orders/<int:order_id>')
    @login_required()
    def order_detail(order_id):
        order = api('GET', f'/orders/{order_id}')
        menu = menu_data()
        return render_template('order.html', order=order, menu=menu,
                               pizza_map={p['id']: p for p in menu['pizzas']},
                               drink_map={d['id']: d for d in menu['drinks']},
                               topping_map={t['id']: t for t in menu['toppings']})

    @app.get('/orders/<int:order_id>/poll')
    @login_required()
    def poll(order_id):
        order = api('GET', f'/orders/{order_id}')
        return jsonify(status=order['status'], label=STATUSES.get(order['status'], order['status']))

    @app.get('/staff')
    @login_required(staff=True)
    def staff():
        offset = max(0, request.args.get('offset', 0, type=int))
        result = api('GET', '/orders', params={'offset': offset, 'limit': 25})
        return render_template('orders.html', orders=result, offset=offset, staff_view=True)

    @app.route('/staff/customers')
    @login_required(staff=True)
    def customers():
        q = request.args.get('q', '')[:254]
        offset = max(0, request.args.get('offset', 0, type=int))
        result = api('GET', '/staff/customers', params={'q': q, 'offset': offset, 'limit': 25})
        return render_template('customers.html', customers=result, q=q, offset=offset)

    @app.post('/staff/customer/select')
    @login_required(staff=True)
    def select_customer():
        session['selected_customer'] = request.form.get('user_id', '')
        return redirect(url_for('cart'))

    @app.post('/staff/orders/<int:order_id>/status')
    @login_required(staff=True)
    def status(order_id):
        value = request.form.get('status')
        if value not in STATUSES:
            abort(400)
        api('PATCH', f'/orders/{order_id}/status', {'status': value})
        flash('Order status updated.', 'success')
        return redirect(url_for('order_detail', order_id=order_id))

    return app


app = create_app()

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000)
