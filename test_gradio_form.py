#!/usr/bin/env python
"""Minimal Gradio form test to debug None parameter issue"""
import gradio as gr

def test_handler(style, lyrics, cot):
    print(f"Received: style={repr(style)}, lyrics={repr(lyrics)}, cot={repr(cot)}")
    if not style:
        raise gr.Error("Style is empty!")
    return f"Got: {style}"

with gr.Blocks() as demo:
    style_input = gr.Textbox(label="Style", value="test style")
    lyrics_input = gr.Textbox(label="Lyrics", value="test lyrics")
    cot_input = gr.Radio(label="Mode", choices=["full", "melody", "off"], value="full")
    btn = gr.Button("Test")
    output = gr.Textbox(label="Output")
    
    btn.click(fn=test_handler, inputs=[style_input, lyrics_input, cot_input], outputs=output)

demo.launch(server_name="127.0.0.1", server_port=9899, show_error=True)
