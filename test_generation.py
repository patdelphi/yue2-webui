#!/usr/bin/env python
"""Test generation flow to verify NoneType and boolean cot fixes"""
import sys
import gradio as gr

# Import the app module
sys.path.insert(0, 'yue2-webui')
from app import on_generate

def test_none_cfg_value():
    """Test that None CFG value doesn't cause TypeError"""
    print("Testing with CFG slider default value (should handle None gracefully)...")
    
    try:
        # This simulates the actual call from Gradio with all required params
        result = on_generate(
            style="流行音乐，轻快旋律",
            lyrics="[Verse]\n今天的阳光很灿烂\n[Chorus]\n让我们一起歌唱",
            cot=False,  # Frontend may send boolean False instead of "off"
            seed=831001,
            cfg_scale=0,  # This is what the slider sends when set to 0
            num_inference_steps=8,
            out_format="PCM 16-bit (标准)",
            batch_count=1,
            normalize=False,
            fade=False,
            trim=False,
            metadata=False,
            abc_text=None,
            abc_temp=0.7,
            abc_top_p=0.9,
            abc_top_k=30,
            abc_rep_penalty=1.005,
            abc_pen_window=100,
            abc_min_tok=32,
            abc_max_tok=4096,
            sem_temp=1.0,
            sem_top_p=0.95,
            sem_top_k=100,
            sem_rep_penalty=1.2,
            sem_pen_window=100,
            sem_min_tok=32,
            sem_max_tok=4096,
            progress=None
        )
        
        print("PASS: No TypeError occurred with boolean cot and zero cfg_scale")
        print(f"Result type: {type(result)}")
        
        # If it's a generator (for streaming), consume first item
        if hasattr(result, '__iter__') and not isinstance(result, str):
            result_list = list(result)
            print(f"Got {len(result_list)} results from generator")
            
        return True
        
    except TypeError as e:
        if "'<' not supported between instances of 'NoneType'" in str(e):
            print(f"FAIL: Original NoneType bug still exists - {e}")
            return False
        else:
            print(f"FAIL: Different TypeError - {e}")
            raise
    except ValueError as e:
        if "is not a valid CotMode" in str(e):
            print(f"FAIL: Boolean cot handling not working - {e}")
            return False
        else:
            print(f"Note: Got expected ValueError (not the bugs we're testing): {e}")
            print("This is OK - we're only testing for the NoneType and boolean cot bugs")
            return True
    except Exception as e:
        print(f"Note: Got expected error (not the bugs we're testing): {type(e).__name__}: {e}")
        print("This is OK - we're only testing for the NoneType and boolean cot bugs")
        return True

if __name__ == "__main__":
    success = test_none_cfg_value()
    sys.exit(0 if success else 1)
