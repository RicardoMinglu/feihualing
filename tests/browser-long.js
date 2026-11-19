async page => {
 const checks=[];const check=(n,v)=>{if(!v)throw Error(n);checks.push(n)};
 await page.goto('http://127.0.0.1:8765');
 const created=await page.evaluate(async()=>{
  const post=async(path,body)=>{const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...body,request_id:crypto.randomUUID()})});if(!r.ok)throw Error(await r.text());return r.json()};
  let g=(await post('/api/games',{word:'月',difficulty:'endless'})).game,used=new Set(),offset=0,rows=[];
  for(let turn=0;turn<110;turn++){
   if(!rows.some(r=>!used.has(r.id))){rows=(await(await fetch('/api/search?word=月&limit=50&offset='+offset)).json()).items;offset+=50}
   const line=rows.find(r=>!used.has(r.id));const response=await post('/api/games/'+g.id+'/answers',{text:line.text});g=response.game;
   if(response.verdict.kind!=='accepted')throw Error('not accepted');g.messages.forEach(m=>m.line_ids.forEach(id=>used.add(id)));
  }
  localStorage.setItem('feihualing.game',g.id);return g;
 });
 check('110 real rounds',created.round===110&&created.status==='playing');await page.reload();await page.locator('.poem-message').last().waitFor();
 check('restore bounded DOM',await page.locator('.poem-message').count()===50);
 await page.locator('#history').evaluate(e=>e.scrollTop=0);await page.getByRole('button',{name:'查看更早记录'}).click();await page.waitForFunction(()=>document.querySelectorAll('.poem-message').length===100);
 check('older loaded',await page.locator('.poem-message').first().getAttribute('data-seq')==='121');
 for(const count of [150,200]){await page.locator('#history').evaluate(e=>e.scrollTop=0);await page.getByRole('button',{name:'查看更早记录'}).click();await page.waitForFunction(n=>document.querySelectorAll('.poem-message').length===n,count)}
 await page.locator('#history').evaluate(e=>e.scrollTop=0);await page.getByRole('button',{name:'查看更早记录'}).click();await page.waitForFunction(()=>document.querySelector('.poem-message')?.dataset.seq==='1');
 check('long history DOM capped',await page.locator('.poem-message').count()===200);
 await page.getByRole('button',{name:'回到最新 ↓'}).click();await page.waitForFunction(()=>document.querySelector('.poem-message:last-of-type')?.dataset.seq==='220');
 check('jump latest exact',true);
 await page.context().setOffline(true);await page.getByRole('textbox',{name:'输入诗句'}).fill('床前明月光');await page.getByRole('button',{name:'提交诗句'}).click();await page.getByRole('button',{name:'重试提交'}).waitFor();
 check('offline retry',true);await page.context().setOffline(false);await page.getByRole('button',{name:'重试提交'}).click();await page.waitForFunction(()=>!document.querySelector('.send-btn').disabled);check('retry keeps game',(await page.locator('.battle-meta').innerText()).includes('110'));
 await page.getByRole('button',{name:'退出对局'}).click();await page.getByRole('button',{name:'离开对局',exact:true}).click();await page.waitForURL('**#home');return {checks};
}
