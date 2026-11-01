from rest_framework import authentication, exceptions


class FirebaseAuthentication(authentication.BaseAuthentication):
    """`Authorization: Bearer <Firebase ID token>` for the mobile app."""
    keyword = 'Bearer'

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).decode()
        if not header.startswith(self.keyword + ' '):
            return None
        token = header[len(self.keyword) + 1:].strip()
        try:
            from firebase_admin import auth
            claims = auth.verify_id_token(token)
        except Exception:  # noqa: BLE001
            raise exceptions.AuthenticationFailed('Invalid or expired Firebase ID token.')
        from basic.services.identity import user_from_firebase_claims
        user = user_from_firebase_claims(claims)
        if user is None or not user.is_active:
            raise exceptions.AuthenticationFailed('Account is not available.')
        return user, claims

    def authenticate_header(self, request):
        return self.keyword
