import json

with open("/tmp/pebbling_vs_caterpillar_8anr5mdx/adder16_pebbling_debug.json") as f:
    data = json.load(f)

schedule = data["gate_level_schedule"]
gate1_events = [(e["step"], e["op"]) for e in schedule if e["gate_id"] == 1]
print(f"Gate 1 toggles: {len(gate1_events)}")
for step, op in gate1_events:
    # show what else is happening in the same step
    same_step = [e for e in schedule if e["step"] == step]
    others = [f"{e['gate_id']}:{e['op']}" for e in same_step if e["gate_id"] != 1]
    print(f"  step {step}: gate1 {op}  |  concurrent: {others}")
