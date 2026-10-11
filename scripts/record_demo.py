"""Cut the existing real browser recordings into a 35-second silent demo.

No network requests, model calls, or synthesized speech. Original browser clips
must already exist in reports/demo/01_problem.mp4 ... 06_export.mp4. The saved
original instrumental music master becomes the only audio track. The captured compile replay
remains explicitly labelled, and live-call evidence is preserved in metadata.

Usage: .venv/bin/python scripts/record_demo.py [--music MUSIC_PATH]
"""
from __future__ import annotations
import argparse
import asyncio
import html
import json
from pathlib import Path
import subprocess
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'reports/demo'
# start_s, duration_s, source clip, title, explanatory caption, optional crop
SCENES = [
    (0, 5, '01_problem', 'Websites are built for people.',
     'Give agents reusable tools.', None),
    (0, 5, '02_website', 'Open a book. Read its details.',
     'BeFreed public website · actual browser navigation', None),
    (.8, 6, '03_incremental', 'Discover. Verify. Then call.',
     'Real compile replay · waiting compressed', None),
    (0, 6, '04_schema', 'One input: the book slug.',
     'Request and extraction rules are saved.', '1000:400:140:210'),
    (0, 8, '05_call', 'Call the real site. Get structured data.',
     'HTTP 200 · 1 book · no model at runtime', '1000:540:140:0'),
    (0, 5, '06_export', 'Compile once. Reuse anywhere.',
     'MCP · OpenAPI · llms.txt', '960:430:160:175'),
]

async def make_caption_images():
    # Render typography locally because the installed ffmpeg lacks drawtext.
    async with async_playwright() as p:
        browser=await p.chromium.launch()
        page=await browser.new_page(viewport={'width':1280,'height':104})
        for _,_,name,title,caption,_ in SCENES:
            await page.set_content(f"""<style>
              *{{box-sizing:border-box}}body{{margin:0;background:#090e18;color:white;
              border-top:2px solid #6ee7b7;padding:18px 34px;font-family:Arial,sans-serif}}
              .title{{font-size:27px;font-weight:600;margin-bottom:12px}}
              .caption{{font-size:19px;color:#cbd5e1}}
              </style><div class=title>{html.escape(title)}</div>
              <div class=caption>{html.escape(caption)}</div>""")
            await page.screenshot(path=str(OUT/f'{name}-caption.png'))
        await browser.close()

def run(*args):
    subprocess.run(list(args), check=True)

def probe(path):
    return json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_format',
                                              '-show_streams', '-of', 'json', str(path)]))

def timestamp(value):
    milliseconds = round(value * 1000)
    return f'{milliseconds//3600000:02d}:{milliseconds//60000%60:02d}:{milliseconds//1000%60:02d},{milliseconds%1000:03d}'

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--music', type=Path, default=OUT/'demo-music.m4a')
    args = parser.parse_args()
    music = args.music.resolve()
    if not music.exists():
        original = OUT/'demo-music.wav'
        import sys
        run(sys.executable, str(ROOT/'scripts/generate_demo_music.py'), str(original), '35')
        music = OUT/'demo-music-normalized.wav'
        run('ffmpeg','-y','-loglevel','error','-i',str(original),'-af','loudnorm=I=-23:TP=-3:LRA=8','-ar','44100',str(music))
    previous = json.loads((OUT/'metadata.json').read_text())
    asyncio.run(make_caption_images())
    inputs=[]
    filters=[]
    scenes=[]
    subtitles=[]
    at=0
    for i,(start,duration,name,title,caption,crop) in enumerate(SCENES):
        source=OUT/f'{name}.mp4'
        if not source.exists():
            raise SystemExit(f'Missing original browser clip: {source}')
        if start+duration > float(probe(source)['format']['duration'])+.05:
            raise SystemExit(f'Source clip too short: {source}')
        inputs.extend(['-i',str(source)])
        transform=f'trim=start={start}:duration={duration},setpts=PTS-STARTPTS,fps=30'
        if crop:
            transform += f',crop={crop},scale=1280:-2,pad=1280:720:0:0:color=0x090b0f'
        # Caption PNGs are opaque and replace the original full footer.
        transform += ',setsar=1,format=yuv420p'
        filters.append(f'[{i}:v]{transform}[base{i}]')
        filters.append(f'[base{i}][{i+len(SCENES)}:v]overlay=0:616:shortest=1:format=auto[v{i}]')
        scenes.append({'name':name,'start_s':at,'duration_s':duration,'source_start_s':start,
                       'source_clip':source.name,'caption':title+' '+caption,'crop':crop})
        subtitles.append(f'{i+1}\n{timestamp(at)} --> {timestamp(at+duration)}\n{title}\n{caption}\n')
        at+=duration
    filters.append(''.join(f'[v{i}]' for i in range(len(SCENES)))+f'concat=n={len(SCENES)}:v=1:a=0[video]')
    for _,_,name,_,_,_ in SCENES:
        inputs.extend(['-loop','1','-i',str(OUT/f'{name}-caption.png')])
    inputs.extend(['-i',str(music)])
    filters.append(f'[{2*len(SCENES)}:a]atrim=duration={at},asetpts=PTS-STARTPTS,afade=t=in:d=0.7,afade=t=out:st={at-1.5}:d=1.5[audio]')
    target=OUT/'shim-befreed-demo.mp4'
    temporary=OUT/'shim-befreed-demo.new.mp4'
    run('ffmpeg','-y','-loglevel','error',*inputs,'-filter_complex',';'.join(filters),
        '-map','[video]','-map','[audio]','-t',str(at),'-c:v','libx264','-preset','fast','-crf','20',
        '-c:a','aac','-b:a','160k','-movflags','+faststart',str(temporary))
    info=probe(temporary)
    assert float(info['format']['duration'])<=60
    run('ffmpeg','-loglevel','error','-i',str(temporary),'-f','null','-')
    temporary.replace(target)
    info['format']['filename'] = str(target)
    metadata={k:v for k,v in previous.items() if k not in {'ffprobe','scenes','narration_language','voice','duration_s'}}
    metadata.update({'duration_s':at,'audio':'Original light instrumental background music; no narration',
                     'narration':False,'caption_language':'en','music_source':music.name,
                     'network_requests_during_reedit':0,'model_calls_during_reedit':0,
                     'scenes':scenes,'ffprobe':info,'full_decode_verified':True})
    (OUT/'metadata.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2))
    (OUT/'shim-befreed-demo.en.srt').write_text('\n'.join(subtitles))
    (OUT/'script-en.md').write_text('# Shim × BeFreed: 35-second demo\n\nMusic and English captions. No voice-over.\n\n'+
        '\n\n'.join(f"{s['start_s']:02d}–{s['start_s']+s['duration_s']:02d}s: {s['caption']}" for s in scenes)+'\n')
    print(json.dumps({'video':str(target),'duration_s':info['format']['duration'],
                     'bytes':info['format']['size'],'audio':'music only; no narration'}))

if __name__=='__main__':
    main()
