"""The existing owner tool can represent a compact review repair."""
import jsonschema
import pytest
from tools.kanban_tools import KANBAN_MANAGE_SCHEMA


def test_compact_review_repair_is_representable_but_partial_maker_plan_is_not():
    args={'task_id':'t_test','action':'work','reason':'Use the installed reviewer','plan':{'assignee':'grace'}}
    jsonschema.validate(args,KANBAN_MANAGE_SCHEMA['parameters'])
    args['plan']={'assignee':'grace','brief':'Partial maker rewrite'}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(args,KANBAN_MANAGE_SCHEMA['parameters'])
