"""Simple owner intent remains simple through both actual tool catalogs."""
import json
import pytest
from jsonschema import validate, ValidationError
from tools.kanban_tools import KANBAN_MANAGE_SCHEMA
from gateway import member_board_tools
from tools.registry import registry


def test_plain_instruction_needs_no_transport_bookkeeping():
    validate({'action':'work','task_id':'t_example','reason':'Finish the source capture with the available web tools.'}, KANBAN_MANAGE_SCHEMA['parameters'])


def test_member_catalog_inherits_same_simple_action_without_board_override():
    member_board_tools.register('simple-work-fixture')
    schema = registry.get_entry('own_work_manage')
    # Use the normal registry API representation used by actual tool loading.
    value = schema.schema if hasattr(schema,'schema') else schema['schema']
    validate({'action':'work','task_id':'t_example','reason':'Work on the existing request.'}, value['parameters'])
    with pytest.raises(ValidationError):
        validate({'action':'work','task_id':'t_example','reason':'Work on it.','board':'other-member'}, value['parameters'])
