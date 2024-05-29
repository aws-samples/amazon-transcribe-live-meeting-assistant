# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import hashlib
import hmac
import datetime
import urllib.parse

class AWSTranscribePresignedURL:
    def __init__(self, access_key: str, secret_key: str,session_token: str, region: str = 'us-east-1'):
        self.access_key = access_key
        self.secret_key = secret_key
        self.session_token = session_token
        self.method = "GET"
        self.service = "transcribe"
        self.region = region
        self.endpoint = ""
        self.host = ""
        self.amz_date = ""
        self.datestamp = ""
        self.canonical_uri = "/stream-transcription-websocket"
        self.canonical_headers = ""
        self.signed_headers = "host"
        self.algorithm = "AWS4-HMAC-SHA256"
        self.credential_scope = ""
        self.canonical_querystring = ""
        self.payload_hash = ""
        self.canonical_request = ""
        self.string_to_sign = ""
        self.signature = ""
        self.request_url = ""
        
    def get_request_url(
        self,
        sample_rate:int,
        language_code: str = "",
        media_encoding: str = "pcm",
        vocabulary_name: str = "",
        session_id: str = "",
        vocabulary_filter_name: str = "",
        vocabulary_filter_method: str = "",
        show_speaker_label: bool = False,
        enable_channel_identification: bool = False,
        number_of_channels: int = 1,
        enable_partial_results_stabilization: bool = False,
        partial_results_stability: str = "",
        content_identification_type: str = "",
        content_redaction_type: str = "",
        pii_entity_types: str = "",
        language_model_name: str = "",
        identify_language: bool = False,
        identify_multiple_languages: bool = False,
        language_options: str = "",
        preferred_language: str = "",
        vocabulary_names: str = "",
        vocabulary_filter_names: str = "",
    ) -> str:
        self.endpoint = f"wss://transcribestreaming.{self.region}.amazonaws.com:8443"
        self.host = f"transcribestreaming.{self.region}.amazonaws.com:8443"

        now = datetime.datetime.now(datetime.timezone.utc)
        self.amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        self.datestamp = now.strftime("%Y%m%d")

        self.canonical_headers = f"host:{self.host}\n"

        self.credential_scope = f"{self.datestamp}%2F{self.region}%2F{self.service}%2Faws4_request"

        self.create_canonical_querystring(
            sample_rate,
            language_code,
            media_encoding,
            vocabulary_name,
            session_id,
            vocabulary_filter_name,
            vocabulary_filter_method,
            show_speaker_label,
            enable_channel_identification,
            number_of_channels,
            enable_partial_results_stabilization,
            partial_results_stability,
            content_identification_type,
            content_redaction_type,
            pii_entity_types,
            language_model_name,
            identify_language,
            identify_multiple_languages,
            language_options,
            preferred_language,
            vocabulary_names,
            vocabulary_filter_names,
        )
        self.create_payload_hash()
        self.create_canonical_request()
        self.create_string_to_sign()
        self.create_signature()
        self.create_url()

        return self.request_url

    def create_canonical_querystring(
        self,
        sample_rate: int,
        language_code: str,
        media_encoding: str,
        vocabulary_name: str,
        session_id: str,
        vocabulary_filter_name: str,
        vocabulary_filter_method: str,
        show_speaker_label: bool,
        enable_channel_identification: bool,
        number_of_channels: int,
        enable_partial_results_stabilization: bool,
        partial_results_stability: str,
        content_identification_type: str,
        content_redaction_type: str,
        pii_entity_types: str,
        language_model_name: str,
        identify_language: bool,
        identify_multiple_languages: bool,
        language_options: str,
        preferred_language: str,
        vocabulary_names: str,
        vocabulary_filter_names: str,
    ):
        self.canonical_querystring = "X-Amz-Algorithm=" + self.algorithm
        self.canonical_querystring += "&X-Amz-Credential=" + self.access_key + "%2F" + self.credential_scope
        self.canonical_querystring += "&X-Amz-Date=" + self.amz_date
        self.canonical_querystring += "&X-Amz-Expires=300"
        if self.session_token:
            self.canonical_querystring += "&X-Amz-Security-Token=" + urllib.parse.quote(self.session_token, safe='')
        self.canonical_querystring += "&X-Amz-SignedHeaders=" + self.signed_headers
        if content_identification_type:
            self.canonical_querystring += "&content-identification-type=" + content_identification_type
        if content_redaction_type:
            self.canonical_querystring += "&content-redaction-type=" + content_redaction_type
        if enable_channel_identification:
            self.canonical_querystring += "&enable-channel-identification=true"
        if enable_partial_results_stabilization:
            self.canonical_querystring += "&enable-partial-results-stabilization=true"
        if identify_language:
            self.canonical_querystring += "&identify-language=true"
        if identify_multiple_languages:
            self.canonical_querystring += "&identify-multiple-languages=true"
        if language_model_name:
            self.canonical_querystring += "&language-model-name=" + language_model_name
        if language_options:
            self.canonical_querystring += "&language-options=" + urllib.parse.quote(language_options, safe='')
        if language_code:
            self.canonical_querystring += "&language-code=" + language_code
        if media_encoding:
            self.canonical_querystring += "&media-encoding=" + media_encoding
        if number_of_channels and number_of_channels > 1:
            self.canonical_querystring += "&number-of-channels=" + str(number_of_channels)
        if partial_results_stability:
            self.canonical_querystring += "&partial-results-stability=" + partial_results_stability
        if pii_entity_types:
            self.canonical_querystring += "&pii-entity-types=" +  urllib.parse.quote(pii_entity_types, safe='')
        if preferred_language:
            self.canonical_querystring += "&preferred-language=" + preferred_language
        if sample_rate:
            self.canonical_querystring += "&sample-rate=" + str(sample_rate)
        if session_id:
            self.canonical_querystring += "&session-id=" + session_id
        if show_speaker_label:
            self.canonical_querystring += "&show-speaker-label=true"
        if vocabulary_filter_method:
            self.canonical_querystring += "&vocabulary-filter-method=" + vocabulary_filter_method
        if vocabulary_filter_name:
            self.canonical_querystring += "&vocabulary-filter-name=" + vocabulary_filter_name
        if vocabulary_names:
            self.canonical_querystring += "&vocabulary-names=" + urllib.parse.quote(vocabulary_names, safe='')
        if vocabulary_name:
            self.canonical_querystring += "&vocabulary-name=" + vocabulary_name
        if vocabulary_filter_names:
            self.canonical_querystring += "&vocabulary-filter-names=" + urllib.parse.quote(vocabulary_filter_names, safe='')

    def create_payload_hash(self):
        self.payload_hash = self.to_hex(self.hash(""))
        
    def create_canonical_request(self):
        print(self.canonical_querystring)
        self.canonical_request = f"{self.method}\n{self.canonical_uri}\n{self.canonical_querystring}\n{self.canonical_headers}\n{self.signed_headers}\n{self.payload_hash}"
        
    def create_string_to_sign(self):
        hashed_canonical_request = self.to_hex(self.hash(self.canonical_request))
        new_credential_scope = f"{self.datestamp}/{self.region}/{self.service}/aws4_request"
        
        self.string_to_sign = f"{self.algorithm}\n{self.amz_date}\n{new_credential_scope}\n{hashed_canonical_request}"
        
    def create_signature(self):
        signing_key = self.get_signature_key(self.secret_key, self.datestamp, self.region, self.service)
        self.signature = self.to_hex(self.get_keyed_hash(signing_key, self.string_to_sign))
        
    def create_url(self):
        self.canonical_querystring += "&X-Amz-Signature=" + self.signature
        self.request_url = self.endpoint + self.canonical_uri + "?" + self.canonical_querystring
    
    @staticmethod
    def hmac_sha256(data: str, key: bytes) -> bytes:
        return hmac.new(key, data.encode('utf-8'), hashlib.sha256).digest()
        
    @staticmethod
    def get_signature_key(key: str, date_stamp: str, region_name: str, service_name: str) -> bytes:
        k_secret = ("AWS4" + key).encode('utf-8')
        k_date = AWSTranscribePresignedURL.hmac_sha256(date_stamp, k_secret)
        k_region = AWSTranscribePresignedURL.hmac_sha256(region_name, k_date)
        k_service = AWSTranscribePresignedURL.hmac_sha256(service_name, k_region)
        k_signing = AWSTranscribePresignedURL.hmac_sha256("aws4_request", k_service)

        return k_signing
    
    @staticmethod
    def hash(value: str) -> bytes:
        return hashlib.sha256(value.encode('utf-8')).digest()
    
    @staticmethod
    def to_hex(data: bytes) -> str:
        return data.hex()
    
    @staticmethod
    def get_keyed_hash(key: bytes, value: str) -> bytes:
        mac = hmac.new(key, value.encode('utf-8'), hashlib.sha256)
        return mac.digest()