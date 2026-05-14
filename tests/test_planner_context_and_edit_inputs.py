import json

import pytest
from fastapi import HTTPException

from backend.app import main

from conftest import write_png


def test_planner_context_uses_only_successful_image_prompts():
    history = [
        {"role": "user", "content": "生成一张森林图", "meta_json": "{}"},
        {
            "role": "assistant",
            "content": "正在处理，失败原因：上游超时",
            "meta_json": json.dumps(
                {
                    "planner_status": "running",
                    "image_status": "failed",
                    "image_prompt": "失败图提示词",
                    "image_error": {"message": "timeout"},
                },
                ensure_ascii=False,
            ),
        },
        {
            "role": "assistant",
            "content": "已完成森林图",
            "meta_json": json.dumps(
                {
                    "planner_status": "done",
                    "image_status": "done",
                    "image_prompt": "一张成功生成的森林图",
                },
                ensure_ascii=False,
            ),
        },
        {
            "role": "assistant",
            "content": "分镜完成",
            "meta_json": json.dumps(
                {
                    "storyboard": {
                        "shots": [
                            {"status": "done", "name": "镜头一", "prompt": "成功镜头提示词"},
                            {"status": "failed", "name": "镜头二", "prompt": "失败镜头提示词"},
                        ]
                    }
                },
                ensure_ascii=False,
            ),
        },
    ]

    prompt = main.build_context_prompt(history, "继续优化")

    assert "一张成功生成的森林图" in prompt
    assert "成功镜头提示词" in prompt
    assert "失败图提示词" not in prompt
    assert "失败原因" not in prompt
    assert "失败镜头提示词" not in prompt


def test_chat_completions_planner_payload_does_not_attach_image_body(tmp_path, monkeypatch):
    image_path = write_png(tmp_path / "ref.png")
    captured = {}

    async def fake_post_chat(payload, **kwargs):
        captured["payload"] = payload
        return {"output": [{"type": "message", "content": [{"type": "output_text", "text": "{}"}]}]}

    monkeypatch.setattr(main, "post_chat_completions", fake_post_chat)

    import asyncio

    asyncio.run(
        main.call_chat_planner(
            model="planner",
            prompt="请参考图片改图",
            config=main.ClientConfig(base_url="https://planner.example/v1", api_key="sk-planner"),
            uploaded=[(image_path, "image/png")],
            image_contexts=[{"source": "upload", "ref": "upload:1"}],
            planner_endpoint="chat_completions",
        )
    )

    content = captured["payload"]["messages"][0]["content"]
    assert "chat/completions 兼容模式只传文字说明" in content
    assert "data:image" not in content
    assert "input_image" not in json.dumps(captured["payload"], ensure_ascii=False)


def test_edit_input_bundle_requires_and_orders_target_before_reference(tmp_path):
    target = write_png(tmp_path / "target.png")
    reference = write_png(tmp_path / "reference.png")

    edit_inputs, notes, reference_candidates, target_candidates = main.build_edit_input_bundle(
        [(target, "image/png"), (reference, "image/png")],
        ["edit_target", "reference"],
    )

    assert [path.name for path, _mime in edit_inputs] == ["target.png", "reference.png"]
    assert "直接修改目标图" in notes[0]
    assert "辅助参考图" in notes[1]
    assert len(target_candidates) == 1
    assert len(reference_candidates) == 1

    with pytest.raises(HTTPException) as exc:
        main.build_edit_input_bundle(
            [(target, "image/png"), (reference, "image/png")],
            ["reference", "reference"],
        )
    assert exc.value.status_code == 400
