"""P2 localhost-only lab: stub only provider/SMS HTTP, not ERP document methods."""
from urllib.parse import urlparse

import requests


def request(method, url, **kwargs):
    host = urlparse(url).hostname
    if host == 'sms.example.test':
        data = {'success': True}
    elif host != 'cybqa.pesapal.com':
        raise AssertionError('Unexpected outbound HTTP host: '+str(host))
    elif url.endswith('Auth/RequestToken'):
        data = dict(status='200', error=None, token='p2-transport-stub-token')
    elif url.endswith('URLSetup/RegisterIPN'):
        data = dict(status='200', error=None, ipn_id='p2-transport-stub-ipn')
    elif url.endswith('Transactions/SubmitOrderRequest'):
        payload = kwargs['json']
        assert payload['amount'] == 12000 and payload['currency'] == 'UGX'
        ref = payload['id']
        print('P2_PSP_ORDER', ref, payload['amount'], payload['currency'], flush=True)
        data = dict(status='200', error=None, order_tracking_id='TRACK-'+ref,
            redirect_url='https://cybqa.pesapal.com/pay/'+ref, merchant_reference=ref)
    elif url.endswith('Transactions/GetTransactionStatus'):
        ref = kwargs['params']['orderTrackingId'].removeprefix('TRACK-')
        data = dict(status='200', error=None, status_code=1, amount=12000,
            currency='UGX', merchant_reference=ref)
    else:
        raise AssertionError('Unexpected provider endpoint')
    class Response:
        status_code = 200
        text = 'P2 disposable transport stub'
        def raise_for_status(self):
            pass
        def json(self):
            return data
    return Response()


requests.request = request
requests.api.request = request
