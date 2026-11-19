async(page)=>{
 const base='http://127.0.0.1:8765';const dest='output/playwright/v130/';const checks=[];
 await page.goto(base);await page.evaluate(()=>localStorage.removeItem('feihualing.game'));await page.reload();await page.getByRole('button',{name:'开始对战',exact:true}).waitFor();
 const poems=await (await page.request.get(base+'/api/search?word='+encodeURIComponent('月')+'&limit=50')).json();
 await page.getByRole('button',{name:'简单 5 轮获胜'}).click();await page.getByRole('button',{name:'开始对战',exact:true}).click();await page.waitForURL('**#battle');
 let used=[],latest;
 for(let i=0;i<5;i++){
  const line=poems.items.find(p=>!used.includes(p.id));await page.getByRole('textbox',{name:'输入诗句'}).fill(line.text);
  const response=page.waitForResponse(r=>r.url().endsWith('/answers')&&r.request().method()==='POST');await page.getByRole('button',{name:'提交诗句',exact:true}).click();latest=await(await response).json();used=latest.game.used;
  if(latest.game.round!==i+1)throw Error('round did not advance');
  if(i<4)await page.waitForFunction(()=>!document.querySelector('.send-btn').disabled);
 }
 if(latest.game.status!=='won')throw Error('game not won');await page.getByRole('dialog',{name:'此局，你胜了'}).waitFor();await page.screenshot({path:dest+'victory.png'});checks.push('five-round victory displayed');
 await page.getByRole('button',{name:'去查诗',exact:true}).click();await page.getByRole('link',{name:'人机对战',exact:true}).click();
 await page.getByRole('button',{name:'困难 15 轮获胜'}).click();await page.getByRole('button',{name:'开始对战',exact:true}).click();await page.waitForURL('**#battle');
 if(!await page.getByRole('button',{name:'提示 · 0'}).isDisabled())throw Error('hard hint enabled');checks.push('hard hints disabled');
 if(await page.locator('#countdown').count())throw Error('unexpected timer');checks.push('hard mode untimed');
 const response=page.waitForResponse(r=>r.url().endsWith('/end'));
 await page.getByRole('button',{name:'退出对局'}).click();await page.getByRole('button',{name:'离开对局',exact:true}).click();
 if((await(await response).json()).game.status!=='abandoned')throw Error('not ended');checks.push('manual end');
 await page.waitForURL('**#home');
 return {checks};
}
