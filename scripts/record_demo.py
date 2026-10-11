"""Record an honest <=60s English Shim demo using actual sites and saved tools.

Run with .venv/bin/python scripts/record_demo.py. Uses installed Playwright,
ffmpeg, and macOS say; no model requests or production writes. The replay scene
is explicitly labelled and uses captured real compile events, not invented data.
"""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'reports/demo'
BASE = 'http://127.0.0.1:8808'
SCENES = [
    (8, '01_problem', 'Websites are built for people. Give agents reusable tools.',
     'Websites are built for people. Agents need reusable tools, with clear inputs, that they can call directly.'),
    (8, '02_website', 'The human workflow: open a book and read its details',
     "Here is BeFreed's public book page. A person opens a book and reads its title, author, and categories."),
    (9, '03_incremental', 'Discover → Verify → Call, while exploration continues',
     'Shim discovers tools, then verifies them before allowing calls. This real compilation replay compresses the waiting time.'),
    (9, '04_schema', 'Saved compilation: reusable tools with typed inputs',
     'Load the saved compilation. Book details become a reusable tool, with a typed slug input and saved extraction rules.'),
    (12, '05_call', 'Live call: HTTP 200 → structured book data',
     'Now call the live website. The response is HTTP two hundred, with structured book data. Repeated calls use HTTP directly, without another model invocation.'),
    (8, '06_export', 'Compile once. Reuse through MCP / OpenAPI / llms.txt.',
     'Compile once. Export OpenAPI and website documentation, and serve the verified tools through MCP.'),
]


async def banner(page, title, subtitle):
    await page.evaluate('''({title,subtitle}) => {
      document.querySelector('#demo-caption')?.remove();
      const el=document.createElement('div'); el.id='demo-caption';
      el.style.cssText='position:fixed;bottom:0;left:0;right:0;z-index:2147483647;padding:14px 34px 17px;background:rgba(7,12,22,.97);color:white;font-family:-apple-system,BlinkMacSystemFont,sans-serif;border-top:2px solid #6ee7b7;pointer-events:none;';
      const a=document.createElement('div');a.textContent=title;a.style.cssText='font-size:25px;font-weight:650;margin-bottom:6px';
      const b=document.createElement('div');b.textContent=subtitle;b.style.cssText='font-size:17px;color:#cbd5e1';el.append(a,b);document.body.append(el);
    }''', {'title': title, 'subtitle': subtitle})

async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    manifest=json.loads((ROOT/'sites/www.befreed.ai/manifest.json').read_text())
    action=next(a for a in manifest['actions'] if '/book/{' in a['backend']['url'] and a['check']['passed'])
    slug=action['check']['input']['slug']
    bookurl=action['backend']['url'].replace('{slug}',slug)
    event_source = OUT/'compile-events.jsonl'
    if not event_source.exists():
        event_source = Path('/tmp/shim-streaming-live.jsonl')
    events=[json.loads(line) for line in event_source.read_text().splitlines()]
    events=[e for e in events if 'at_s' in e]
    (OUT/'compile-events.jsonl').write_text('\n'.join(json.dumps(e,ensure_ascii=False) for e in events)+'\n')
    metadata={'duration_s':sum(x[0] for x in SCENES), 'source_site':bookurl,
              'saved_compiled_at':manifest['compiled_at'], 'tool':action['name'],
              'input':{'slug':slug}, 'replay':'real captured compilation, waiting compressed', 'scenes':[], 'narration_language':'en-US', 'voice':'Samantha'}
    only = os.environ.get('SHIM_DEMO_SCENE')
    if only and (OUT/'metadata.json').exists():
        metadata = json.loads((OUT/'metadata.json').read_text())
        metadata['scenes'] = [s for s in metadata['scenes'] if s['name'] != only]
    async with async_playwright() as p:
      browser=await p.chromium.launch()
      for duration,name,title,narration in SCENES:
        if only and name != only:
          continue
        ctx=await browser.new_context(viewport={'width':1280,'height':720},
             device_scale_factor=1,record_video_dir=str(OUT/'raw'),
             record_video_size={'width':1280,'height':720})
        page=await ctx.new_page()
        source_started=time.monotonic()
        if name=='03_incremental':
          replay=[]
          for ev in events:
            t=ev.pop('at_s')
            delay = 0.3+t*.6 if t<2 else 2.2 if t<69.5 else 3.3 if t<75 else 5+(t-75)*.12
            replay.append({'delay':delay,'event':ev})
          await page.add_init_script('''window.EventSource=class {
             constructor(url){this.closed=false;const events=EVENTS;
               for(const e of events)setTimeout(()=>{if(!this.closed&&this.onmessage)this.onmessage({data:JSON.stringify(e.event)})},e.delay*1000);}
             close(){this.closed=true;}
          };'''.replace('EVENTS',json.dumps(replay)))
        await page.goto('https://www.befreed.ai' if name=='01_problem' else bookurl if name=='02_website' else BASE, wait_until='domcontentloaded', timeout=60000)
        await page.wait_for_timeout(1400)
        if name=='01_problem':
          await page.evaluate('window.scrollTo(0,140)')
        elif name=='02_website':
          await page.locator('article h1').wait_for(timeout=15000)
          await page.locator('article h1').scroll_into_view_if_needed()
        elif name=='03_incremental':
          await page.locator('#url').fill('https://www.befreed.ai')
          await page.locator('#intent').fill('Learn public categories and book details')
        else:
          await page.locator('#library button').filter(has_text='www.befreed.ai').click()
          card=page.locator('.card').filter(has=page.locator('.tool',has_text=action['name'])).first
          await card.wait_for()
          if name=='06_export':
            await page.get_by_text('Produced files',exact=True).scroll_into_view_if_needed()
          else:
            await card.scroll_into_view_if_needed()
            if name=='04_schema':
              await card.locator('summary').click()
            else:
              await card.locator('input').fill(slug)
        await banner(page,title, 'Real compilation replay · waiting compressed · no new model calls' if name=='03_incremental' else
                     'Saved, completed compilation · no current exploration run' if name in ('04_schema','06_export') else
                     'Actual website request · called live during this recording' if name=='05_call' else 'Public website · actual browser navigation')
        trim_start=time.monotonic()-source_started
        if name=='03_incremental':
          await page.locator('#go').click()
          await page.wait_for_timeout(1800)
          await page.locator('#results').scroll_into_view_if_needed()
          await page.wait_for_timeout(3500)
          await page.locator('.card[data-tool-state="verified"]').first.scroll_into_view_if_needed()
          await page.wait_for_timeout(3700)
        elif name=='05_call':
          start=time.monotonic()
          async with page.expect_response(lambda r: '/api/call' in r.url and r.request.method=='POST') as promise:
            await card.get_by_role('button',name='Call',exact=True).click()
          response=await promise.value
          data=await response.json()
          metadata['call']={'http_transport_status':response.status, 'elapsed_s':round(time.monotonic()-start,3), **data}
          assert response.status==200 and data['status']==200 and data['items_found']>=1, data
          await card.locator('pre.out').last.evaluate('(el) => window.scrollTo(0, window.scrollY + el.getBoundingClientRect().top - 170)')
          await page.wait_for_timeout(max(1,duration-(time.monotonic()-start))*1000)
        else:
          await page.wait_for_timeout(duration*1000)
        await page.screenshot(path=str(OUT/f'{name}.png'))
        video=page.video
        await ctx.close()
        raw=Path(await video.path())
        wav=OUT/f'{name}.aiff'
        subprocess.run(['say','-v','Samantha','-r','170','-o',str(wav),narration],check=True)
        audio_duration=float(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration','-of','default=nw=1:nk=1',str(wav)]))
        # Gentle speed adjustment if a scene narration exceeds its allotted time.
        tempo=max(1,audio_duration/(duration-.3))
        target=OUT/f'{name}.mp4'
        subprocess.run(['ffmpeg','-y','-loglevel','error','-ss',str(trim_start),'-i',str(raw),'-i',str(wav),
           '-t',str(duration),'-vf','fps=30,format=yuv420p','-af',f'atempo={tempo:.4f},apad',
           '-c:v','libx264','-preset','fast','-crf','20','-c:a','aac','-movflags','+faststart',str(target)],check=True)
        metadata['scenes'].append({'name':name,'duration_s':duration,'narration':narration,'source_trim_s':round(trim_start,3),'narration_language':'en-US','voice':'Samantha','source_audio_duration_s':audio_duration,'audio_tempo':tempo})
      await browser.close()
    concat=OUT/'clips.txt'
    concat.write_text(''.join(f"file '{name}.mp4'\n" for _,name,_,_ in SCENES))
    subprocess.run(['ffmpeg','-y','-loglevel','error','-f','concat','-safe','0','-i',str(concat),'-c','copy','-movflags','+faststart',str(OUT/'shim-befreed-demo.mp4')],check=True)
    info=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_format','-show_streams','-of','json',str(OUT/'shim-befreed-demo.mp4')]))
    assert float(info['format']['duration'])<=60
    metadata['ffprobe']=info
    (OUT/'metadata.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2))
    subtitle_parts=[]
    at=0
    def subtitle_time(t):
        return f'{int(t)//3600:02d}:{int(t)//60%60:02d}:{int(t)%60:02d},000'
    for i,(duration,name,title,narration) in enumerate(SCENES,1):
        subtitle_parts.append(f'{i}\n{subtitle_time(at)} --> {subtitle_time(at+duration)}\n{narration}\n')
        at += duration
    (OUT/'shim-befreed-demo.en.srt').write_text('\n'.join(subtitle_parts))
    (OUT/'script-en.md').write_text('# Shim × BeFreed: 54-second demo\n\n'+'\n\n'.join(f'{i+1}. {name}({duration} seconds)\n\n{narration}' for i,(duration,name,title,narration) in enumerate(SCENES))+'\n')
    print(json.dumps({'video':str(OUT/'shim-befreed-demo.mp4'),'duration':info['format']['duration'],'tool_call_status':metadata['call']['status'],'records':metadata['call']['items_found']},ensure_ascii=False))

if __name__=='__main__':
    asyncio.run(main())
