# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""AppSync GQL Client wrapper for Meeting Assistant streaming"""
from typing import Optional
from urllib.parse import urlparse

from gql.client import Client
from gql.transport.appsync_auth import AppSyncIAMAuthentication
from gql.transport.requests import RequestsHTTPTransport
from requests.auth import AuthBase


class RequestsIamAuth(AuthBase):
    """Requests Sigv4 IAM Auth"""

    def __init__(self, url: str):
        self._host = str(urlparse(url).netloc)
        self._auth = AppSyncIAMAuthentication(host=self._host)

    def __call__(self, r):
        r.headers = self._auth.get_headers(data=r.body.decode("utf-8"))
        return r


class AppsyncRequestsGqlClient(Client):
    """AppSync Requests Gql Client"""

    def __init__(
        self,
        url: str,
        retries: int = 3,
        timeout: Optional[int] = 5,
        **kwargs,
    ):
        auth = RequestsIamAuth(url=url)
        transport = RequestsHTTPTransport(url=url, auth=auth, retries=retries, timeout=timeout)

        super().__init__(transport=transport, **kwargs)
