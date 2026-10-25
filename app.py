"""Server-rendered pizza ordering UI. API credentials never enter browser storage."""
import os
import secrets
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from cachelib.file import FileSystemCache
from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, session, url_for
from flask_session import Session

STATUSES = {
    'order_received': 'Order received', 'preparing': 'Preparing',
    'out_for_delivery': 'Out for delivery', 'ready_for_pickup': 'Ready for pickup',
    'completed': 'Completed',
}


ANALYTICS_RANGES = (7, 30, 90, 365)
MAX_ANALYTICS_ORDERS = 50000


class APIError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def parse_order_time(value):
    placed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return placed if placed.tzinfo else placed.replace(tzinfo=timezone.utc)


def short_date(day):
    return f'{day:%b} {day.day}'


def hour_label(hour):
    return f'{hour % 12 or 12} {"AM" if hour < 12 else "PM"}'


def bar_chart(values, labels, unit, tick_every=1, width=720, height=180):
    """Geometry for a plain SVG bar chart; no inline styles so the strict CSP still applies."""
    top = max(values, default=0) or 1
    slot = width / max(len(values), 1)
    gap = min(4, slot * .25)
    bars, ticks = [], []
    for index, (value, label) in enumerate(zip(values, labels)):
        bar_height = round(height * value / top, 1)
        bars.append(dict(x=round(index * slot + gap / 2, 1), y=round(height - bar_height, 1),
                         width=round(slot - gap, 1), height=bar_height, label=label, value=value))
        if index % tick_every == 0:
            ticks.append(dict(x=round(index * slot + slot / 2, 1), text=label.removeprefix('Week of ')))
    return dict(width=width, height=height, bars=bars, ticks=ticks, top=max(values, default=0), unit=unit)


def ranked(counter, limit=5):
    rows = counter.most_common(limit)
    return [dict(name=name, count=count, top=rows[0][1]) for name, count in rows]


def build_analytics(orders, menu, days, tz, now=None):
    """Summarise orders placed in the last `days` days (in `tz`). Revenue uses current menu prices."""
    today = (now or datetime.now(timezone.utc)).astimezone(tz).date()
    first = today - timedelta(days=days - 1)
    pizzas = {p['id']: p for p in menu['pizzas']}
    drinks = {d['id']: d for d in menu['drinks']}
    toppings = {t['id']: t['name'] for t in menu['toppings']}
    daily = {first + timedelta(days=i): [0, Decimal('0')] for i in range(days)}
    hours, status_counts = [0] * 24, Counter()
    pizza_counts, drink_counts, topping_counts = Counter(), Counter(), Counter()
    count, revenue, items = 0, Decimal('0'), 0
    for order in orders:
        local = parse_order_time(order['order_time']).astimezone(tz)
        if not first <= local.date() <= today:
            continue
        total = Decimal('0')
        for line in order.get('pizzas', []):
            pizza, quantity = pizzas.get(line['pizza_id']), line['quantity']
            items += quantity
            if pizza:
                total += Decimal(pizza['price']) * quantity
            pizza_counts[pizza['size'].title() + ' pizza' if pizza else 'Removed pizza'] += quantity
            for topping_id in line.get('topping_ids', []):
                topping_counts[toppings.get(topping_id, 'Removed topping')] += quantity
        for line in order.get('drinks', []):
            drink, quantity = drinks.get(line['drink_id']), line['quantity']
            items += quantity
            if drink:
                total += Decimal(drink['price']) * quantity
            drink_counts[f"{drink['name']} · {drink['size']}" if drink else 'Removed drink'] += quantity
        count += 1
        revenue += total
        hours[local.hour] += 1
        status_counts[order['status']] += 1
        daily[local.date()][0] += 1
        daily[local.date()][1] += total
    step = 7 if days > 90 else 1
    rows = []
    for start in range(0, days, step):
        bucket = [daily[first + timedelta(days=i)] for i in range(start, min(start + step, days))]
        label = short_date(first + timedelta(days=start))
        rows.append(dict(label=('Week of ' + label) if step > 1 else label,
                         orders=sum(b[0] for b in bucket), revenue=sum((b[1] for b in bucket), Decimal('0'))))
    return dict(
        orders=count, revenue=revenue, average=(revenue / count) if count else Decimal('0'), items=items,
        open_orders=count - status_counts['completed'], step=step, rows=rows,
        daily_chart=bar_chart([r['orders'] for r in rows], [r['label'] for r in rows], 'orders',
                              tick_every=max(1, len(rows) // 6)),
        hour_chart=bar_chart(hours, [hour_label(h) for h in range(24)], 'order', tick_every=6),
        statuses=[dict(name=label, count=status_counts[key], top=max(count, 1)) for key, label in STATUSES.items()],
        pizzas=ranked(pizza_counts), drinks=ranked(drink_counts), toppings=ranked(topping_counts),
        busiest=(hour_label(hours.index(max(hours))) if count else None),
    )


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True,
                instance_path=os.environ.get('INSTANCE_PATH') or None)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    app.config.update(
        SECRET_KEY=os.environ.get('SECRET_KEY') or secrets.token_hex(32),
        API_BASE_URL=os.environ.get('API_BASE_URL', 'http://127.0.0.1:8000').rstrip('/'),
        API_CSRF_PROTECTION=os.environ.get('API_CSRF_PROTECTION', '1'),
        BUSINESS_TIMEZONE=os.environ.get('BUSINESS_TIMEZONE', 'America/Chicago'),
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

    def all_items(path, page=100, max_items=None):
        result = []
        while True:
            batch = api('GET', path, params={'offset': len(result), 'limit': page})
            result.extend(batch)
            if len(batch) < page or (max_items and len(result) >= max_items):
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

    def inventory_payload():
        name, unit = request.form.get('name', '').strip(), request.form.get('unit', '').strip()
        if not 1 <= len(name) <= 50:
            abort(400, description='Enter an ingredient name of up to 50 characters.')
        if not 1 <= len(unit) <= 10:
            abort(400, description='Enter a unit of up to 10 characters, such as kg, lb, or boxes.')
        message = 'Quantity must be 0 or more, below 1,000,000,000, with at most one decimal place.'
        try:
            amount = Decimal(request.form.get('quantity', '').strip())
        except InvalidOperation:
            abort(400, description=message)
        if not amount.is_finite() or not 0 <= amount < Decimal('1e9') or amount != amount.quantize(Decimal('0.1')):
            abort(400, description=message)
        return {'name': name, 'unit': unit, 'quantity': f"{amount.quantize(Decimal('0.1')):f}"}

    def inventory_result(action, success):
        try:
            action()
        except APIError as error:
            if error.status == 401:
                raise
            flash(str(error), 'error')
        else:
            flash(success, 'success')
        return redirect(url_for('inventory', q=request.form.get('q', '')[:50] or None))

    @app.get('/staff/inventory')
    @login_required(staff=True)
    def inventory():
        q = request.args.get('q', '').strip()[:50]
        items = sorted(all_items('/inventory'), key=lambda item: item['name'].lower())
        for item in items:
            item['out'] = Decimal(item['quantity']) == 0
        shown = [item for item in items if q.lower() in item['name'].lower()]
        return render_template('inventory.html', items=shown, q=q, total=len(items),
                               out_count=sum(item['out'] for item in items))

    @app.post('/staff/inventory')
    @login_required(staff=True)
    def inventory_create():
        payload = inventory_payload()
        return inventory_result(lambda: api('POST', '/inventory', payload), f"Added {payload['name']}.")

    @app.post('/staff/inventory/<int:item_id>')
    @login_required(staff=True)
    def inventory_update(item_id):
        payload = inventory_payload()
        return inventory_result(lambda: api('PUT', f'/inventory/{item_id}', payload), f"Saved {payload['name']}.")

    @app.post('/staff/inventory/<int:item_id>/delete')
    @login_required(staff=True)
    def inventory_delete(item_id):
        return inventory_result(lambda: api('DELETE', f'/inventory/{item_id}'), 'Ingredient removed.')

    @app.get('/staff/analytics')
    @login_required(staff=True)
    def analytics():
        days = request.args.get('days', 30, type=int)
        if days not in ANALYTICS_RANGES:
            days = 30
        try:
            tz = ZoneInfo(app.config['BUSINESS_TIMEZONE'])
        except (ZoneInfoNotFoundError, ValueError):
            tz = timezone.utc
        orders = all_items('/orders', page=1000, max_items=MAX_ANALYTICS_ORDERS)
        data = build_analytics(orders, menu_data(), days, tz)
        return render_template('analytics.html', data=data, days=days, ranges=ANALYTICS_RANGES,
                               tz_name=getattr(tz, 'key', 'UTC'),
                               truncated=len(orders) >= MAX_ANALYTICS_ORDERS)

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
