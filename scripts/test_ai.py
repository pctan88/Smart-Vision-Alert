import os
import glob
from config.settings import settings
from core.analyzer import SafetyAnalyzer
import time

def test_ai():
    print("Initializing Gemini Safety Analyzer...")
    analyzer = SafetyAnalyzer(settings)
    
    event_dir = "captures/20260509_2100_2200/561559273962491392"
    
    if not os.path.exists(event_dir):
        print(f"Directory not found: {event_dir}")
        return

    # Find all jpeg files
    all_files = sorted(glob.glob(os.path.join(event_dir, "*.jpg")))
    
    if not all_files:
        print("No images found in the directory.")
        return
        
    # Group files by segment label (e.g. "first", "last", "t0060s")
    groups = {}
    for f in all_files:
        basename = os.path.basename(f)
        label = basename.split("_")[0]
        if label not in groups:
            groups[label] = []
        groups[label].append(f)
        
    print(f"Found {len(groups)} segments to analyze.\n")
    
    # Just test the first 3 segments to avoid hitting API limits
    test_labels = list(groups.keys())[:3]
    print(f"Testing the first 3 segments: {test_labels}\n")
    print("="*60)
    
    for label in test_labels:
        paths = sorted(groups[label])
        print(f"Analyzing Segment: [{label}] ({len(paths)} frames)")
        
        # Call the multi-frame analyzer
        result = analyzer.analyze_multi_frame(paths)
        
        print(f"Status:       {'✅ SAFE' if result.is_safe else '⚠️ UNSAFE'}")
        print(f"Risk Level:   {result.risk_level.upper()}")
        print(f"Motion:       {'Detected' if result.motion_detected else 'NO MOTION'}")
        print(f"Stillness:    {'WARNING' if result.stillness_warning else 'None'}")
        print(f"Description:  {result.description}")
        print(f"Temporal:     {result.temporal_description}")
        if result.detected_hazards:
            print(f"Hazards:      {', '.join(result.detected_hazards)}")
            
        print("-" * 60)
        time.sleep(2) # Brief pause to respect API rate limits

if __name__ == "__main__":
    test_ai()
