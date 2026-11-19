async page => {
 const checks=[],errors=[];page.on('pageerror',e=>errors.push(e.message));
 const check=(label,ok)=>{if(!ok)throw Error(label);checks.push(label)};
 await page.route('**/assets/**',r=>r.continue());
 await page.goto('http://127.0.0.1:8765');await page.evaluate(()=>localStorage.removeItem('feihualing.game'));await page.reload();
 const wait=()=>page.waitForFunction(()=>!document.querySelector('.send-btn')?.disabled&&!document.querySelector('[data-action="start"]')?.disabled);
 const leave=async()=>{await page.getByRole('button',{name:'退出对局'}).click();await page.getByRole('button',{name:'离开对局',exact:true}).click();await page.waitForURL('**#home')};
 const send=async text=>{await page.getByRole('textbox',{name:'输入诗句'}).fill(text);const promise=page.waitForResponse(r=>r.url().endsWith('/answers'));await page.getByRole('button',{name:'提交诗句'}).click();const r=await(await promise).json();await wait();return r};
 const screen=async label=>{
  check(label+' no overflow',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  check(label+' no clipped text',await page.evaluate(()=>![...document.querySelectorAll('.segment,.word-input,.result-line,.poem-text,.result-title')].some(e=>e.scrollWidth>e.clientWidth+2)));
 };
 for(const [theme,name] of Object.entries({a:'清雅诗笺',b:'墨韵擂台',c:'晴山诗会'})){
  await page.getByRole('button',{name:'界面主题'}).click();await page.getByRole('radio',{name,exact:true}).check();await page.getByRole('button',{name:'完成',exact:true}).click();
  for(const width of [360,390,430,1440]){
   await page.setViewportSize({width,height:width===1440?1000:844});await screen(theme+'/home/'+width);
   check('word padding '+theme+width,await page.locator('.word-input').evaluate(e=>getComputedStyle(e).paddingTop==='0px'&&e.clientHeight>=70));
   check('four modes '+theme+width,await page.locator('[data-level]').count()===4);
   if(width===390){await page.mouse.move(0,0);await page.screenshot({path:'output/playwright/v130/'+theme+'-home.png'})}
   await page.getByRole('link',{name:'查诗',exact:true}).click();await page.locator('.result-item').first().waitFor();await screen(theme+'/search/'+width);
   check('search footer visible '+theme+width,await page.locator('.search-footer').evaluate(e=>e.getBoundingClientRect().bottom<=innerHeight+1));
   await page.getByRole('button',{name:'查看更多',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('.result-item').length===40);
   await page.locator('#search-results').evaluate(e=>e.scrollTop=e.scrollHeight);
   check('search input stays visible '+theme+width,await page.locator('#search-word').evaluate(e=>e.getBoundingClientRect().top>=0));
   check('start stays visible '+theme+width,await page.locator('[data-action="search-start"]').evaluate(e=>e.getBoundingClientRect().bottom<=innerHeight));
   await page.locator('#search-results').evaluate(e=>e.scrollTop=0);
   if(width===390){await page.mouse.move(0,0);await page.screenshot({path:'output/playwright/v130/'+theme+'-search.png'})}
   await page.getByRole('link',{name:'人机对战'}).click();await page.locator('[data-level="endless"]').click();await page.getByRole('button',{name:'开始对战',exact:true}).click();await page.waitForURL('**#battle');await wait();
   const r=await send('床前明月光');check('real reply '+theme+width,r.verdict.kind==='accepted');
   await screen(theme+'/battle/'+width);
   check('no timer '+theme+width,await page.locator('#countdown').count()===0&&(await page.locator('.battle-meta').innerText()).includes('不限时'));
   const geometry=await page.locator('#history').evaluate(e=>{const p=e.querySelector('.player').getBoundingClientRect(),c=e.querySelector('.computer').getBoundingClientRect();return p.left>c.left&&p.right>c.right});check('left right '+theme+width,geometry);
   if(width===390){await page.mouse.move(0,0);await page.screenshot({path:'output/playwright/v130/'+theme+'-battle.png'})}
   await leave();
   // Reset query pagination before the next viewport.
   await page.getByRole('link',{name:'查诗',exact:true}).click();await page.getByRole('button',{name:'查诗',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('.result-item').length===20);await page.getByRole('link',{name:'人机对战'}).click();
  }
 }
 await page.setViewportSize({width:390,height:844});await page.getByRole('link',{name:'查诗',exact:true}).click();
 await page.getByRole('button',{name:'作者',exact:true}).click();await page.getByRole('textbox',{name:'作者姓名'}).fill('李白');await page.getByRole('button',{name:'查诗',exact:true}).click();await page.locator('.result-item').first().waitFor();
 check('author count',(await page.locator('.results-bar').innerText()).includes('首作品'));check('author no start',await page.locator('[data-action="search-start"]').count()===0);
 await page.getByRole('button',{name:'诗句',exact:true}).click();await page.getByRole('textbox',{name:'诗句关键词'}).fill('明月光，疑是');await page.getByRole('button',{name:'查诗',exact:true}).click();await page.locator('.result-item').first().waitFor();
 check('phrase hit',(await page.locator('.result-item').first().innerText()).includes('静夜思'));check('phrase highlighted',(await page.locator('.result-line mark').innerText()).includes('疑是'));
 await page.locator('.result-open').first().click();await page.getByRole('dialog').waitFor();check('full poem',(await page.getByRole('dialog').innerText()).includes('李白'));await page.getByRole('button',{name:'关闭',exact:true}).click();
 await page.getByRole('link',{name:'人机对战'}).click();await page.getByRole('button',{name:'开始对战',exact:true}).click();await page.waitForURL('**#battle');await wait();
 let rows=await page.evaluate(async()=>await(await fetch('/api/search?word=月&limit=50')).json()),used=new Set();
 for(let i=0;i<18;i++){const line=rows.items.find(r=>!used.has(r.id));const r=await send(line.text);r.game.messages.forEach(m=>m.line_ids.forEach(id=>used.add(id)));check('endless round '+i,r.game.round===i+1&&r.game.status==='playing');await page.waitForFunction(()=>{const h=document.querySelector('#history');return h.scrollHeight-h.clientHeight-h.scrollTop<4})}
 check('auto bottom',true);
 await page.locator('#history').evaluate(e=>e.scrollTop=0);await page.waitForFunction(()=>!document.querySelector('.latest-btn').hidden);
 const position=await page.locator('#history').evaluate(e=>e.scrollTop);await page.waitForTimeout(4300);check('poll preserves reading',Math.abs(await page.locator('#history').evaluate(e=>e.scrollTop)-position)<3);
 await page.getByRole('button',{name:'回到最新 ↓',exact:true}).click();await page.waitForFunction(()=>{const h=document.querySelector('#history');return h.scrollHeight-h.clientHeight-h.scrollTop<4});
 for(let i=0;i<3;i++){await page.getByRole('button',{name:'提示 · 不限次'}).click();await page.getByRole('button',{name:'继续接令'}).click()}
 check('unlimited hints',true);await page.reload();await page.locator('.poem-message').last().waitFor();await page.waitForFunction(()=>{const h=document.querySelector('#history');return h.scrollHeight-h.clientHeight-h.scrollTop<4});check('restored bottom',true);
 await page.setViewportSize({width:390,height:460});await page.getByRole('textbox',{name:'输入诗句'}).focus();check('keyboard composer',await page.locator('.send-btn').evaluate(e=>e.getBoundingClientRect().bottom<=innerHeight+1));
 await page.setViewportSize({width:390,height:844});await leave();
 check('no script errors',errors.length===0);return {count:checks.length,checks,errors};
}
