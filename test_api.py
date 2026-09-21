#!/usr/bin/env python
"""Test on_generate API endpoint"""
import requests
import json

url = "http://127.0.0.1:9898/gradio_api/call/on_generate"

data = {
    "data": [
        "流行音乐，轻快旋律",  # style
        "[Verse]\n今天的阳光很灿烂\n[Chorus]\n让我们一起歌唱",  # lyrics
        "full",  # cot
        831001,  # seed
        0,  # cfg_scale
        8,  # num_inference_steps
        "pcm16",  # out_format
        1,  # batch_count
        False,  # normalize
        False,  # fade
        False,  # trim
        False,  # metadata
        "",  # abc_text
        0.7,  # abc_temp
        0.9,  # abc_top_p
        30,  # abc_top_k
        1.005,  # abc_rep_penalty
        100,  # abc_pen_window
        32,  # abc_min_tok
        4096,  # abc_max_tok
        1.0,  # sem_temp
        0.95,  # sem_top_p
        100,  # sem_top_k
        1.2,  # sem_rep_penalty
        50,  # sem_pen_window
        200,  # sem_min_tok
        9000,  # sem_max_tok
    ]
}

print(f"Sending {len(data['data'])} parameters to {url}")
print(f"First 3 params: style={data['data'][0]!r}, lyrics={data['data'][1]!r}, cot={data['data'][2]!r}")

response = requests.post(url, json=data)
print(f"Status: {response.status_code}")
print(f"Response: {response.text}")
