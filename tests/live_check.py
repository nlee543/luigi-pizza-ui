"""Opt-in checks that create test records in a non-production API.

Run from the repository root with `python -m tests.live_check` (or runpy).
Credentials are read from JSON on stdin, never from source files.
"""
import json
import secrets
import sys
from datetime import datetime, timezone

import requests
from cachelib import SimpleCache

from app import create_app


def main():
    credentials = json.load(sys.stdin)
    app = create_app({'TESTING': True, 'SESSION_CACHELIB': SimpleCache()})
    admin, customer, fresh = (app.test_client() for _ in range(3))
    created_orders = []
    checks = []

    def check(condition, label):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS:', label, flush=True)

    def post(client, path, **data):
        client.get('/login')
        with client.session_transaction() as s:
            data['csrf_token'] = s['csrf']
        return client.post(path, data=data)

    def login(client, account):
        response = post(client, '/login', **account)
        with client.session_transaction() as s:
            authenticated = bool(s.get('user'))
        check(response.status_code == 302 and authenticated, 'Login succeeds')

    def api(client, method, path, payload=None):
        with client.session_transaction() as s:
            cookies, csrf = s['api_cookies'], s['api_csrf']
        return requests.request(method, app.config['API_BASE_URL'] + path,
                                cookies=cookies, headers={'X-CSRF-Token': csrf},
                                json=payload, timeout=15)

    try:
        login(admin, credentials['admin'])
        login(customer, credentials['customer'])
        with customer.session_transaction() as s:
            customer_id = s['user']['id']
        check(admin.get('/staff').status_code == 200, 'Admin kitchen dashboard renders')
        check(customer.get('/staff').status_code == 403, 'Customer blocked from staff dashboard')
        check(admin.get('/staff/customers', query_string={'q': credentials['customer']['email']}).status_code == 200,
              'Staff customer search renders')
        check(customer.post('/cart/add', data={}).status_code == 400, 'Missing form CSRF rejected')

        pizzas = requests.get(app.config['API_BASE_URL'] + '/pizzas', timeout=15).json()
        drinks = requests.get(app.config['API_BASE_URL'] + '/drinks', timeout=15).json()
        toppings = requests.get(app.config['API_BASE_URL'] + '/toppings', timeout=15).json()
        check(bool(pizzas and drinks and toppings), 'Live menu has pizza, drink, and topping records')
        for client, label in ((customer, 'Customer'), (admin, 'Staff')):
            check(post(client, '/cart/add', kind='pizzas', item_id=str(pizzas[0]['id']),
                       quantity='2', topping_ids=str(toppings[0]['id'])).status_code == 302,
                  label + ' pizza customization added')
            post(client, '/cart/add', kind='drinks', item_id=str(drinks[0]['id']), quantity='1')
            check(client.get('/cart').status_code == 200, label + ' basket renders')
            if client is admin:
                check(post(client, '/staff/customer/select', user_id=str(customer_id)).status_code == 302,
                      'Staff selects customer')
            with client.session_transaction() as s:
                token = s['checkout_token']
            response = post(client, '/checkout', checkout_token=token, user_id=str(customer_id))
            check(response.status_code == 302 and '/orders/' in (response.location or ''), label + ' order submitted')
            order_id = int(response.location.rsplit('/', 1)[1])
            created_orders.append(order_id)
            order_response = api(admin, 'GET', f'/orders/{order_id}')
            check(order_response.status_code == 200, label + ' order exists in API')
            order = order_response.json()
            check(order['user_id'] == customer_id and order['pizzas'][0]['quantity'] == 2
                  and order['pizzas'][0]['topping_ids'] == [toppings[0]['id']]
                  and order['drinks'][0]['quantity'] == 1, label + ' order ownership and contents match')
            with client.session_transaction() as s:
                check(order['created_by_user_id'] == s['user']['id'], label + ' creator recorded correctly')
                check(not s['cart'], label + ' basket cleared after success')
            check(post(client, '/checkout', checkout_token=token, user_id=str(customer_id)).status_code == 400,
                  label + ' repeated checkout rejected')
            check(customer.get(f'/orders/{order_id}').status_code == 200, label + ' order detail renders for customer')
            check(customer.get(f'/orders/{order_id}/poll').json['status'] == 'order_received', label + ' received status visible')
            check(post(customer, f'/staff/orders/{order_id}/status', status='completed').status_code == 403,
                  'Customer cannot change order status')
            branch = 'ready_for_pickup' if client is customer else 'out_for_delivery'
            for status in ('preparing', branch, 'completed'):
                check(post(admin, f'/staff/orders/{order_id}/status', status=status).status_code == 302,
                      label + ' status update: ' + status)
                check(customer.get(f'/orders/{order_id}/poll').json['status'] == status,
                      label + ' tracker reflects: ' + status)

        email = 'ui-test-' + secrets.token_hex(6) + '@example.com'
        old_password, new_password = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        response = post(fresh, '/register', email=email, password=old_password, name='UI Integration Test',
                        address='123 Test Street', phone_number='555-0100', dine_in_name='UI Test')
        check(response.status_code == 302, 'Temporary customer registration succeeds')
        login(fresh, {'email': email, 'password': old_password})
        with fresh.session_transaction() as s:
            temporary_id = s['user']['id']
        for order_id in created_orders:
            check(fresh.get(f'/orders/{order_id}').status_code in (403, 404), 'Other customer cannot view order')
            check(fresh.get(f'/orders/{order_id}/poll').status_code in (403, 404), 'Other customer cannot poll order')
        check(post(fresh, '/account/password', current_password=old_password,
                   new_password=new_password, confirm_password=new_password).status_code == 302,
              'Temporary customer password change succeeds')
        login(fresh, {'email': email, 'password': new_password})
        check(post(fresh, '/logout').status_code == 302, 'Temporary customer logout succeeds')
        response = post(fresh, '/login', email=email, password=old_password)
        with fresh.session_transaction() as s:
            check(response.status_code == 200 and not s.get('user'), 'Old password no longer works')
        check(api(admin, 'PATCH', f'/admin/users/{temporary_id}/access', {'role': 'customer', 'active': False}).status_code == 200,
              'Temporary test account deactivated after checks')
        print(json.dumps({'checked_at': datetime.now(timezone.utc).isoformat(), 'checks_passed': len(checks),
                          'completed_order_ids': created_orders, 'deactivated_test_user_id': temporary_id}), flush=True)
    finally:
        for client in (fresh, customer, admin):
            with client.session_transaction() as s:
                authenticated = bool(s.get('user'))
            if authenticated:
                post(client, '/logout')
        print('Test sessions logged out. Test order IDs: ' + str(created_orders), flush=True)


if __name__ == '__main__':
    main()
