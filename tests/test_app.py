from copy import deepcopy
from unittest.mock import patch

import pytest
import requests
from cachelib import SimpleCache

from app import create_app

USER = dict(id=1, name='Test Customer', email='customer@example.test', role='customer',
            active=True, must_change_password=False, address='123 Test Street', phone_number='555-0100', dine_in_name='Test')
MENU = {'/pizzas': [dict(id=1, size='large', price='9.00')],
        '/toppings': [dict(id=7, name='Pepperoni')],
        '/drinks': [dict(id=1, name='Coke', size='small', price='1.00')]}
INVENTORY = [dict(id=2, name='mozzarella', unit='kg', quantity='0.0'), dict(id=1, name='Flour', unit='kg', quantity='12.5')]
ORDER = dict(id=10, user_id=1, created_by_user_id=1, status='preparing', order_time='2026-10-24T12:00:00Z',
             pizzas=[dict(pizza_id=1, quantity=2, topping_ids=[7])], drinks=[])


@pytest.fixture
def setup():
    app = create_app({'TESTING': True, 'SECRET_KEY': 'test-key', 'SESSION_CACHELIB': SimpleCache()})
    client = app.test_client()
    calls = []
    state = {'user': deepcopy(USER), 'failure': None}

    def api_call(http, method, url, **kwargs):
        path = url.removeprefix('http://127.0.0.1:8000')
        calls.append((method, path, kwargs))
        response = requests.Response()
        response.status_code = 200
        payload = None
        if state['failure'] and (not state.get('failure_path') or (method, path) == state['failure_path']):
            response.status_code = state['failure']
            payload = {'detail': 'Request denied'}
        elif path in MENU:
            payload = MENU[path]
        elif path in ('/auth/login', '/auth/me'):
            http.cookies.set('luigi_session', 'server-secret')
            payload = {'user': state['user'], 'csrf_token': 'api-token'}
        elif path == '/auth/register':
            response.status_code = 201
            payload = state['user']
        elif path in ('/auth/logout', '/auth/change-password'):
            response.status_code = 204
        elif path == '/staff/customers':
            payload = [USER]
        elif path == '/inventory' and method == 'GET':
            payload = INVENTORY
        elif path == '/inventory' and method == 'POST':
            response.status_code = 201
            payload = dict(id=3, **kwargs['json'])
        elif path.startswith('/inventory/') and method == 'PUT':
            payload = dict(id=int(path.rsplit('/', 1)[1]), **kwargs['json'])
        elif path.startswith('/inventory/') and method == 'DELETE':
            response.status_code = 204
            return response
        elif path == '/orders' and method == 'GET':
            payload = [ORDER]
        elif path in ('/orders', '/staff/orders'):
            payload = ORDER
        elif path.startswith('/orders/'):
            payload = ORDER
        else:
            pytest.fail(f'Unexpected API call: {method} {path}')
        import json
        response._content = json.dumps(payload).encode()
        return response

    with patch('requests.Session.request', autospec=True, side_effect=api_call):
        yield client, calls, state


def post(client, path, **data):
    client.get('/login')
    with client.session_transaction() as session:
        data['csrf_token'] = session['csrf']
    return client.post(path, data=data)


def sign_in(client):
    return post(client, '/login', email='customer@example.test', password='test-password')


def test_public_menu_and_csrf(setup):
    client, calls, _ = setup
    response = client.get('/')
    assert response.status_code == 200
    assert b'Large pizza' in response.data
    assert b'$9.00' in response.data
    assert client.post('/cart/add', data={'kind': 'pizzas', 'item_id': 1}).status_code == 400
    assert not any(call[0] == 'POST' for call in calls)


def test_anonymous_and_customer_staff_access(setup):
    client, _, _ = setup
    for route in ('/orders', '/staff', '/staff/customers', '/account/password'):
        assert client.get(route).status_code == 302
    assert client.get('/orders/10/poll').status_code == 401
    sign_in(client)
    assert client.get('/staff').status_code == 403
    assert post(client, '/staff/orders/10/status', status='completed').status_code == 403


def test_login_keeps_secrets_on_server_and_rotates_session(setup):
    client, calls, _ = setup
    client.get('/login')
    before = client.get_cookie('luigi_ui').value
    response = sign_in(client)
    assert response.status_code == 302
    assert client.get_cookie('luigi_ui').value != before
    assert 'server-secret' not in str(response.headers)
    assert 'api-token' not in str(response.headers)
    assert calls[-1][2]['headers']['X-CSRF-Protection'] == '1'
    client.get('/orders')
    assert calls[-1][2]['headers']['X-CSRF-Token'] == 'api-token'


def test_customer_checkout_and_tracker(setup):
    client, calls, _ = setup
    post(client, '/cart/add', kind='pizzas', item_id='1', quantity='2', topping_ids='7')
    sign_in(client)
    assert b'$18.00' in client.get('/cart').data
    with client.session_transaction() as session:
        token = session['checkout_token']
    response = post(client, '/checkout', checkout_token=token)
    assert response.location.endswith('/orders/10')
    created = [c for c in calls if c[:2] == ('POST', '/orders')]
    assert created[0][2]['json'] == {'pizzas': [{'pizza_id': 1, 'quantity': 2, 'topping_ids': [7]}], 'drinks': []}
    assert post(client, '/checkout', checkout_token=token).status_code == 400
    assert client.get('/orders/10').status_code == 200
    assert client.get('/orders/10/poll').json == {'status': 'preparing', 'label': 'Preparing'}
    assert client.get('/orders').status_code == 200


def test_staff_order_uses_staff_endpoint(setup):
    client, calls, state = setup
    state['user']['role'] = 'staff'
    sign_in(client)
    assert client.get('/staff').status_code == 200
    assert client.get('/staff/customers?q=Test').status_code == 200
    post(client, '/cart/add', kind='drinks', item_id='1', quantity='1')
    client.get('/cart')
    with client.session_transaction() as session:
        token = session['checkout_token']
    assert post(client, '/checkout', checkout_token=token, user_id='1').status_code == 302
    created = [c for c in calls if c[:2] == ('POST', '/staff/orders')]
    assert created[0][2]['json']['user_id'] == 1
    assert post(client, '/staff/orders/10/status', status='completed').status_code == 302
    assert calls[-1][:2] == ('PATCH', '/orders/10/status')
    assert calls[-1][2]['json'] == {'status': 'completed'}


def test_cart_validation_and_removal(setup):
    client, _, _ = setup
    assert post(client, '/cart/add', kind='pizzas', item_id='1', topping_ids='999').status_code == 400
    assert post(client, '/cart/add', kind='pizzas', item_id='1', quantity='-1').status_code == 400
    post(client, '/cart/add', kind='drinks', item_id='1', quantity='1')
    assert post(client, '/cart/update', index='-1', quantity='2').status_code == 400
    assert post(client, '/cart/update', index='0', quantity='0').status_code == 302
    assert b'Your next favorite is waiting' in client.get('/cart').data


def test_registration_password_and_logout(setup):
    client, calls, state = setup
    assert post(client, '/register', email='new@example.test', name='New', address='Address',
                phone_number='555', dine_in_name='New', password='long-password').status_code == 302
    assert calls[-1][:2] == ('POST', '/auth/register')
    state['user']['must_change_password'] = True
    assert sign_in(client).location.endswith('/account/password')
    assert client.get('/orders').location.endswith('/account/password')
    assert client.get('/account/password').status_code == 200
    assert post(client, '/account/password', current_password='old', new_password='new-password', confirm_password='different').status_code == 200
    assert post(client, '/account/password', current_password='old', new_password='new-password', confirm_password='new-password').status_code == 302
    with client.session_transaction() as session:
        assert 'api_cookies' not in session
    sign_in(client)
    assert post(client, '/logout').status_code == 302
    with client.session_transaction() as session:
        assert 'user' not in session


def test_upstream_failure_and_expiry(setup):
    client, _, state = setup
    state['failure'] = 503
    assert client.get('/').status_code == 503
    state['failure'] = None
    sign_in(client)
    state['failure'] = 401
    assert client.get('/orders/10/poll').status_code == 401
    with client.session_transaction() as session:
        assert 'user' not in session


def test_order_failure_preserves_basket(setup):
    client, _, state = setup
    sign_in(client)
    post(client, '/cart/add', kind='pizzas', item_id='1')
    client.get('/cart')
    with client.session_transaction() as session:
        token = session['checkout_token']
    state['failure'] = 503
    state['failure_path'] = ('POST', '/orders')
    response = post(client, '/checkout', checkout_token=token)
    assert response.status_code == 503
    assert b'Check your order history' in response.data
    with client.session_transaction() as session:
        assert len(session['cart']) == 1


def sign_in_staff(setup):
    client, calls, state = setup
    state['user']['role'] = 'staff'
    sign_in(client)
    return client, calls, state


def test_inventory_and_analytics_require_staff(setup):
    client, calls, _ = setup
    for route in ('/staff/inventory', '/staff/analytics'):
        assert client.get(route).status_code == 302
    sign_in(client)
    for route in ('/staff/inventory', '/staff/analytics'):
        assert client.get(route).status_code == 403
    for route in ('/staff/inventory', '/staff/inventory/1', '/staff/inventory/1/delete'):
        assert post(client, route, name='Salt', unit='kg', quantity='1').status_code == 403
    assert not any(call[1].startswith('/inventory') for call in calls)


def test_inventory_list_sorted_with_out_of_stock(setup):
    client, _, _ = sign_in_staff(setup)
    page = client.get('/staff/inventory').data.decode()
    assert page.index('Flour') < page.index('mozzarella')
    assert '1 out of stock' in page and 'Out of stock' in page
    assert 'Flour' in client.get('/staff/inventory?q=flo').data.decode()
    assert 'mozzarella' not in client.get('/staff/inventory?q=flo').data.decode()


def test_inventory_create_update_delete(setup):
    client, calls, _ = sign_in_staff(setup)
    assert post(client, '/staff/inventory', name=' Basil ', unit='kg', quantity='4.5', q='ba').status_code == 302
    assert calls[-1][:2] == ('POST', '/inventory')
    assert calls[-1][2]['json'] == {'name': 'Basil', 'unit': 'kg', 'quantity': '4.5'}
    assert post(client, '/staff/inventory/1', name='Flour', unit='lb', quantity='7').status_code == 302
    assert calls[-1][:2] == ('PUT', '/inventory/1')
    assert calls[-1][2]['json'] == {'name': 'Flour', 'unit': 'lb', 'quantity': '7.0'}
    response = post(client, '/staff/inventory/1/delete')
    assert response.status_code == 302 and calls[-1][:2] == ('DELETE', '/inventory/1')
    assert b'Ingredient removed.' in client.get('/staff/inventory').data


def test_inventory_rejects_invalid_input_before_calling_api(setup):
    client, calls, _ = sign_in_staff(setup)
    before = len(calls)
    bad = [dict(name='', unit='kg', quantity='1'), dict(name='x' * 51, unit='kg', quantity='1'),
           dict(name='Salt', unit='', quantity='1'), dict(name='Salt', unit='x' * 11, quantity='1'),
           dict(name='Salt', unit='kg', quantity='-1'), dict(name='Salt', unit='kg', quantity='1.25'),
           dict(name='Salt', unit='kg', quantity='abc'), dict(name='Salt', unit='kg', quantity='NaN'),
           dict(name='Salt', unit='kg', quantity='1e12')]
    for fields in bad:
        assert post(client, '/staff/inventory', **fields).status_code == 400, fields
    assert not [c for c in calls[before:] if c[1] == '/inventory']


def test_inventory_api_error_is_flashed(setup):
    client, _, state = sign_in_staff(setup)
    state['failure'], state['failure_path'] = 409, ('PUT', '/inventory/1')
    assert post(client, '/staff/inventory/1', name='Flour', unit='kg', quantity='1').status_code == 302
    state['failure'] = None
    assert b'Request denied' in client.get('/staff/inventory').data


def test_analytics_page_and_ranges(setup):
    client, calls, _ = sign_in_staff(setup)
    page = client.get('/staff/analytics?days=365')
    assert page.status_code == 200
    assert ('GET', '/orders') in [c[:2] for c in calls]
    assert client.get('/staff/analytics?days=abc').status_code == 200
    assert b'Last 30 days' in client.get('/staff/analytics?days=5').data


def test_build_analytics_counts_and_estimates():
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from app import build_analytics
    menu = {'pizzas': MENU['/pizzas'], 'toppings': MENU['/toppings'], 'drinks': MENU['/drinks']}
    now = datetime(2026, 10, 24, 20, 0, tzinfo=timezone.utc)
    drink_order = dict(id=11, user_id=1, created_by_user_id=1, status='completed', pizzas=[],
                       drinks=[dict(drink_id=1, quantity=3)], order_time='2026-10-24T01:30:00Z')
    old = dict(ORDER, id=12, order_time='2026-01-01T12:00:00Z')
    data = build_analytics([ORDER, drink_order, old], menu, 7, ZoneInfo('America/Chicago'), now=now)
    assert data['orders'] == 2 and data['items'] == 5 and data['open_orders'] == 1
    assert str(data['revenue']) == '21.00' and str(data['average']) == '10.50'
    assert data['pizzas'] == [dict(name='Large pizza', count=2, top=2)]
    assert data['toppings'][0]['name'] == 'Pepperoni' and data['drinks'][0]['count'] == 3
    # 01:30 UTC on the 24th is 8:30 PM Central on the 23rd.
    assert [r['orders'] for r in data['rows']][-2:] == [1, 1]
    assert data['hour_chart']['bars'][20]['value'] == 1 and data['busiest'] in ('7 AM', '12 PM', '8 PM')
    weekly = build_analytics([ORDER], menu, 365, ZoneInfo('UTC'), now=now)
    assert weekly['step'] == 7 and len(weekly['rows']) == 53
