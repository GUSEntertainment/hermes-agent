"""Inactive server-only adapter to existing request lifecycle.
Route composition must recover identity from existing opaque link reservation,
never submitted body. No browser/model-supplied identity or target accepted.
"""
from pin_commit import submit,Denied

def submit_reserved_pin(*, registry, reserved_identity, request_id, body, phone_registry, canonical, acl):
 if type(body)!=dict or set(body)!={'pin','confirmation'}:raise Denied('PIN fields required')
 def current(request):
  row=registry(reserved_identity)
  binding=row['binding']
  if binding['request']!=request:raise Denied('Reservation changed')
  return binding
 return submit(phone_registry,request_id=request_id,pin=body['pin'],confirmation=body['confirmation'],current_request=current,canonical=canonical,acl=acl)
