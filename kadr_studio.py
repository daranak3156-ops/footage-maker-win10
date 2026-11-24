import json, os, queue, re, subprocess, sys, threading, time
from pathlib import Path
from tkinter import filedialog, messagebox
import customtkinter as ctk
import requests
from PIL import Image
from imageio_ffmpeg import get_ffmpeg_exe
import yt_dlp
from lumean_client import speech as lumean_speech, templates as lumean_templates
from kadr_media import bing_people_chrome, tmdb_people, save_photos, make_speech, probe_duration

ROOT = Path.home() / 'Videos' / 'KadrStudio'

def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True, creationflags=0x08000000 if os.name=='nt' else 0)
    if p.returncode: raise RuntimeError(p.stderr[-1800:] or 'Ошибка обработки видео')
    return p

def api(key, endpoint, **kwargs):
    r=requests.post('https://api.openai.com/v1/'+endpoint, headers={'Authorization':'Bearer '+key}, timeout=300, **kwargs)
    if not r.ok: raise RuntimeError(f'API: {r.status_code}. {r.text[:350]}')
    return r.json()

def rewrite(text, key, model):
    data=api(key,'chat/completions',json={'model':model,'reasoning_effort':'medium','messages':[{'role':'system','content':'Ты редактор русскоязычных документальных роликов. Перепиши исходный текст своими словами, сохрани факты, имена, смысл и примерно длину. Не добавляй вымышленных фактов. Разбей результат на 6–30 коротких сцен. Верни только JSON с массивом scenes. Каждая сцена: text (русский текст озвучки до 1000 символов), query (точный поисковый запрос фотографии на английском), person (полное имя упомянутой личности, либо пустая строка). Не включай рекламу и призывы подписаться.'},{'role':'user','content':text}], 'response_format':{'type':'json_object'}})
    scenes=json.loads(data['choices'][0]['message']['content'])['scenes']
    if not isinstance(scenes,list) or not scenes: raise ValueError('API вернул пустой сценарий')
    for s in scenes:
        if not isinstance(s,dict) or not s.get('text') or not s.get('query'): raise ValueError('Неполная сцена в ответе API')
        if len(s['text'])>4096: raise ValueError('Слишком длинная сцена в ответе API')
    return scenes

def render_scene(photo,audio,out,vertical):
    w,h=(720,1280) if vertical else (1280,720)
    duration=probe_duration(audio)
    filt=f'scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=0x101827,setsar=1,format=yuv420p'
    run([get_ffmpeg_exe(),'-y','-hide_banner','-loglevel','error','-loop','1','-i',str(photo),'-i',str(audio),'-vf',filt,'-t',str(duration),'-r','25','-c:v','libx264','-preset','veryfast','-c:a','aac','-shortest',str(out)])

def build_project(url,manual,cfg,log,stop):
    project=ROOT/time.strftime('%Y%m%d-%H%M%S')
    project.mkdir(parents=True,exist_ok=True)
    def check():
        if stop.is_set(): raise RuntimeError('Остановлено. Все созданные материалы сохранены.')
    def save(data): (project/'project.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    log('Папка проекта: '+str(project))
    state={'url':url,'scenes':[]}; save(state)
    text=manual.strip()
    if not text:
        log('1 / 5 · Загружаю аудио по ссылке…')
        options={'format':'bestaudio/best','outtmpl':str(project/'source.%(ext)s'),'noplaylist':True,'quiet':True,'socket_timeout':40,'ffmpeg_location':get_ffmpeg_exe()}
        with yt_dlp.YoutubeDL(options) as dl:
            info=dl.extract_info(url,download=True)
            source=Path(dl.prepare_filename(info))
        check()
        log('Распознаю речь по API…')
        run([get_ffmpeg_exe(),'-y','-hide_banner','-loglevel','error','-i',str(source),'-vn','-ac','1','-ar','16000','-c:a','libmp3lame','-b:a','48k','-f','segment','-segment_time','600',str(project/'transcribe-%03d.mp3')])
        parts=[]
        for audio in sorted(project.glob('transcribe-*.mp3')):
            check()
            with audio.open('rb') as f: parts.append(api(cfg['key'],'audio/transcriptions',files={'file':(audio.name,f,'audio/mpeg')},data={'model':'whisper-1'})['text'])
        text='\n'.join(parts)
    if not text.strip(): raise ValueError('В видео не удалось найти речь. Вставьте текст вручную.')
    (project/'original.txt').write_text(text,encoding='utf-8'); check()
    log('2 / 5 · Переписываю сценарий…')
    scenes=rewrite(text,cfg['key'],cfg['model']); state['scenes']=scenes; save(state)
    (project/'script.txt').write_text('\n\n'.join(s['text'] for s in scenes),encoding='utf-8')
    clips=[]
    for index,scene in enumerate(scenes):
        check(); log(f'3 / 5 · Фотографии: сцена {index+1} из {len(scenes)}')
        query=scene['query']; results=[]
        if cfg['tmdb'] and scene.get('person'):
            try: results+=tmdb_people(scene['person'],cfg['tmdb'],4)
            except Exception as e: log('TMDB: '+str(e)[:130])
        cache=save_photos(query,[])
        if cache: photos=cache
        else:
            try: results+=bing_people_chrome(query,6)
            except Exception as e: log('Bing: '+str(e)[:150])
            photos=save_photos(query,results)
        if not photos: raise RuntimeError('Не найдены фотографии: '+query+'. Проект сохранён. Проверьте Chrome и доступ к Bing.')
        scene['photo']=photos[index%len(photos)]; save(state)
        check(); log(f'4 / 5 · API-озвучка: сцена {index+1} из {len(scenes)}')
        audio=project/f'voice-{index:03d}.mp3'
        lumean_speech(cfg['lumean'],cfg['template'],scene['text'],audio,stop,log)
        check(); log(f'5 / 5 · Монтаж: сцена {index+1} из {len(scenes)}')
        clip=project/f'clip-{index:03d}.mp4'
        render_scene(scene['photo']['path'],audio,clip,cfg['vertical']); clips.append(clip)
    check()
    (project/'concat.txt').write_text(''.join("file '"+p.name+"'\n" for p in clips),encoding='utf-8')
    out=project/'Kadr.mp4'
    run([get_ffmpeg_exe(),'-y','-hide_banner','-loglevel','error','-f','concat','-safe','0','-i',str(project/'concat.txt'),'-c','copy','-movflags','+faststart',str(out)])
    state['output']=str(out); save(state)
    (project/'sources.txt').write_text('\n'.join(s['photo']['page'] for s in scenes),encoding='utf-8')
    return out

class Studio(ctk.CTk):
    def __init__(self):
        super().__init__(); ctk.set_appearance_mode('dark')
        self.title('КАДР STUDIO · Видео из ссылки'); self.geometry('1160x820'); self.minsize(980,740)
        self.configure(fg_color='#0b1020'); self.events=queue.Queue(); self.stop=threading.Event(); self.output=None
        self.grid_columnconfigure(1,weight=1); self.grid_rowconfigure(0,weight=1)
        side=ctk.CTkFrame(self,width=240,fg_color='#111a2e',corner_radius=0); side.grid(row=0,column=0,sticky='nsew'); side.grid_propagate(False)
        ctk.CTkLabel(side,text='КАДР',font=('Segoe UI',32,'bold'),text_color='#9ea8ff').pack(anchor='w',padx=24,pady=(30,0))
        ctk.CTkLabel(side,text='STUDIO  /  WINDOWS',font=('Segoe UI',12),text_color='#8190aa').pack(anchor='w',padx=26,pady=(0,36))
        ctk.CTkLabel(side,text='Один ролик.\nВесь процесс.',font=('Segoe UI',23,'bold'),justify='left').pack(anchor='w',padx=24)
        for t in ['01   Видео и текст','02   Новый сценарий','03   Фото людей','04   Голос по API','05   Готовый MP4']:
            ctk.CTkLabel(side,text=t,font=('Segoe UI',15),text_color='#b4bed3').pack(anchor='w',padx=24,pady=14)
        ctk.CTkButton(side,text='Открыть мои проекты',fg_color='#24314b',command=self.open_folder).pack(side='bottom',padx=20,pady=24)
        main=ctk.CTkScrollableFrame(self,fg_color='transparent'); main.grid(row=0,column=1,sticky='nsew',padx=28,pady=20)
        ctk.CTkLabel(main,text='Создайте новое видео',font=('Segoe UI',28,'bold')).pack(anchor='w')
        ctk.CTkLabel(main,text='Вставьте ссылку. Программа подготовит сценарий, фотографии, голос и монтаж.',text_color='#94a3bd',wraplength=750,justify='left').pack(anchor='w',pady=(4,20))
        side.grid_remove()
        main.grid(column=0,columnspan=2)
        self.openai_key=ctk.StringVar(value=os.getenv('OPENAI_API_KEY',''))
        settings=ctk.CTkFrame(main,fg_color='#141e32'); settings.pack(fill='x',pady=12)
        self.fields={}
        for label,key,value,secret in [('ССЫЛКА НА ВИДЕО YOUTUBE','url','',False),('TMDB API-ключ · необязательно','tmdb',os.getenv('TMDB_API_KEY',''),True),('Lumean API-ключ · озвучка','lumean',os.getenv('LUMEAN_API_KEY',''),True),('ID шаблона Lumean','template','',False)]:
            row=ctk.CTkFrame(settings,fg_color='transparent'); row.pack(fill='x',padx=18,pady=12)
            ctk.CTkLabel(row,text=label,width=270,anchor='w').pack(side='left')
            e=ctk.CTkEntry(row,show='•' if secret else '',height=42); e.insert(0,value); e.pack(side='left',fill='x',expand=True); self.fields[key]=e
        self.url=self.fields.pop('url')
        ctk.CTkButton(settings,text='Загрузить шаблоны Lumean',command=self.load_templates,width=265,height=42).pack(anchor='w',padx=18,pady=14)
        modelrow=ctk.CTkFrame(main,fg_color='transparent'); modelrow.pack(fill='x',pady=8)
        ctk.CTkLabel(modelrow,text='Модель текста',text_color='#a8b3ff').pack(side='left',padx=(0,12))
        self.models={'GPT-6.2 Astra':'gpt-6.2-astra','GPT-6.2 Sol':'gpt-6.2-sol','GPT-6.1 Sol':'gpt-6.1-sol','GPT-6.1 Terra':'gpt-6.1-terra','GPT-6 Astra':'gpt-6-astra','GPT-6 Sol':'gpt-6-sol','GPT-6 Luna':'gpt-6-luna'}
        self.model_choice=ctk.CTkOptionMenu(modelrow,values=list(self.models),width=190); self.model_choice.set('GPT-6 Astra'); self.model_choice.pack(side='left')
        ctk.CTkLabel(modelrow,text='Усилие: Среднее',text_color='#a8b3ff').pack(side='left',padx=12)
        ctk.CTkButton(modelrow,text='Подключить OpenAI',width=165,fg_color='#28344c',command=self.configure_openai).pack(side='right')
        buttons=ctk.CTkFrame(main,fg_color='transparent'); buttons.pack(fill='x',pady=18)
        self.start=ctk.CTkButton(buttons,text='Создать видео',height=48,width=240,font=('Segoe UI',17,'bold'),fg_color='#6265ed',hover_color='#4e51ca',command=self.launch); self.start.pack(side='left')
        self.cancel=ctk.CTkButton(buttons,text='Остановить',height=48,width=120,fg_color='#28344c',state='disabled',command=self.stop.set); self.cancel.pack(side='left',padx=12)
        self.play=ctk.CTkButton(buttons,text='Открыть MP4',height=48,width=130,fg_color='#1d655e',state='disabled',command=self.open_video); self.play.pack(side='right')
        self.progress=ctk.CTkProgressBar(main,progress_color='#818cf8',height=6); self.progress.pack(fill='x'); self.progress.set(0)
        self.status=ctk.CTkLabel(main,text='Готов к работе',anchor='w',wraplength=740); self.status.pack(fill='x',pady=8)
        self.log=ctk.CTkTextbox(main,height=145,fg_color='#080e1b',text_color='#a9b9d3'); self.log.pack(fill='x'); self.log.configure(state='disabled')
        self.after(150,self.poll)
    def configure_openai(self):
        window=ctk.CTkToplevel(self); window.title('Подключение OpenAI'); window.geometry('540x220'); window.after(100,window.lift)
        ctk.CTkLabel(window,text='API-ключ для рерайта и распознавания видео').pack(padx=20,pady=(20,8))
        ctk.CTkEntry(window,textvariable=self.openai_key,show='•',width=480,height=40).pack(padx=20,pady=8)
        ctk.CTkLabel(window,text='Модель выбирается в главном окне; усилие всегда «Среднее». Озвучка — Lumean.',wraplength=480).pack(padx=20,pady=8)
        ctk.CTkButton(window,text='Готово',command=window.destroy).pack(pady=8)
    def load_templates(self):
        key=self.fields['lumean'].get().strip()
        if not key: return messagebox.showinfo('Lumean','Введите API-ключ Lumean.')
        def work():
            try: self.events.put(('templates',lumean_templates(key)))
            except Exception as e: self.events.put(('log',str(e)))
        threading.Thread(target=work,daemon=True).start()
    def show_templates(self, items):
        if not items: return messagebox.showinfo('Lumean','Создайте шаблон озвучки на сайте Lumean. Шаблоны в папках можно выбрать, вставив их ID вручную.')
        window=ctk.CTkToplevel(self); window.title('Ваши голоса Lumean'); window.geometry('560x420'); window.after(100,window.lift)
        box=ctk.CTkScrollableFrame(window); box.pack(fill='both',expand=True,padx=15,pady=15)
        def choose(item):
            entry=self.fields['template']; entry.delete(0,'end'); entry.insert(0,item['id']); window.destroy()
        for item in items:
            ctk.CTkButton(box,text=item.get('name',item['id'])+' · '+item.get('service_key',''),command=lambda i=item:choose(i)).pack(fill='x',pady=5)
    def open_folder(self):
        ROOT.mkdir(parents=True,exist_ok=True)
        if os.name=='nt': os.startfile(str(ROOT))
    def open_video(self):
        if self.output and os.name=='nt': os.startfile(str(self.output))
    def launch(self):
        cfg={k:v.get().strip() for k,v in self.fields.items()}; cfg.update(vertical=False,key=self.openai_key.get().strip(),model=self.models[self.model_choice.get()])
        url=self.url.get().strip(); manual='' 
        if not cfg['key']: return messagebox.showinfo('API-ключ','Введите OpenAI API-ключ для рерайта и распознавания. Озвучка выполняется Lumean.')
        if not cfg['lumean'] or not cfg['template']: return messagebox.showinfo('Lumean','Введите ключ Lumean и выберите шаблон озвучки.')
        if not manual and not url.startswith(('https://','http://')): return messagebox.showinfo('Источник','Вставьте ссылку на видео или свой текст.')
        self.stop.clear(); self.start.configure(state='disabled'); self.cancel.configure(state='normal'); self.play.configure(state='disabled'); self.progress.configure(mode='indeterminate'); self.progress.start()
        def worker():
            try: self.events.put(('done',build_project(url,manual,cfg,lambda x:self.events.put(('log',x)),self.stop)))
            except Exception as e: self.events.put(('error',str(e)))
        threading.Thread(target=worker,daemon=True).start()
    def poll(self):
        try:
            while True:
                kind,value=self.events.get_nowait()
                if kind=='templates': self.show_templates(value); continue
                if kind in ('done','error'):
                    self.progress.stop(); self.progress.configure(mode='determinate'); self.progress.set(1 if kind=='done' else 0); self.start.configure(state='normal'); self.cancel.configure(state='disabled')
                if kind=='done': self.output=value; self.play.configure(state='normal'); value='Готово: '+str(value)
                if kind=='error': value='Ошибка: '+value
                self.status.configure(text=str(value)); self.log.configure(state='normal'); self.log.insert('end',str(value)+'\n'); self.log.see('end'); self.log.configure(state='disabled')
        except queue.Empty: pass
        self.after(150,self.poll)

if __name__=='__main__':
    if '--self-test' in sys.argv:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p=Path(d); Image.new('RGB',(640,480),'#344479').save(p/'photo.jpg')
            run([get_ffmpeg_exe(),'-y','-f','lavfi','-i','sine=frequency=440:duration=1','-c:a','libmp3lame',str(p/'voice.mp3')])
            render_scene(p/'photo.jpg',p/'voice.mp3',p/'test.mp4',False)
            assert (p/'test.mp4').stat().st_size>1000
        app=Studio(); app.update(); app.destroy()
        sys.exit(0)
    Studio().mainloop()
