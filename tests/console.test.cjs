// DOM-contract tests, not browser rendering tests. Run: node --test tests/console.test.cjs
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
class Element {
  constructor() {this.textContent=''; this.value=''; this.disabled=false; this.handlers={}; this.children=[]; this.classList={toggle(){}};}
  addEventListener(name, callback){this.handlers[name]=callback;}
  replaceChildren(...children){this.children=children;}
  append(...children){this.children.push(...children);}
  click(){this.clicked=true;}
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function setup(fetch) {
  const ids=['access','token','disconnect','refresh','download','connection','requests','success','budget','latency','healing','deployment','business','events','event-count','updated'];
  const nodes=Object.fromEntries(ids.map(id=>[id,new Element()]));
  const context={document:{getElementById:id=>nodes[id], createElement:()=>new Element()}, window:{addEventListener(){}}, fetch, AbortController, URL:{createObjectURL:()=> 'blob:test',revokeObjectURL(){}},setTimeout:fn=>fn()};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../app/static/console.js'),'utf8'),context);
  function connect(value){nodes.token.value=value; nodes.access.handlers.submit({preventDefault(){}});}
  return {nodes, connect};
}
const snapshot={report:{slo:{total_requests:0,success_ratio:null,error_budget_consumed_ratio:null,p95_seconds:null}},application:{version:'0.2.0',fault:null,remaining_seconds:0,fallback_enabled:false},healing:{mode:'simulation',enabled:false,services:{}},deployment:{phase:'idle',active:null,previous:null,candidate:null,last_outcome:'not_deployed',locked:false},events:[]};
const ok=()=>({ok:true,json:async()=>snapshot,blob:async()=>({})});
test('auth failure clears stale content; success stays read-only and clears token input',async()=>{
 const calls=[];
 const app=setup(async(url,opts)=>{calls.push([url,opts]);return opts.headers.Authorization==='Bearer good'?ok():{ok:false,status:401};});
 app.connect('wrong'); await tick(); await tick();
 assert.match(app.nodes.connection.textContent,/令牌无效/);
 app.connect('good'); await tick(); await tick();
 assert.equal(app.nodes.token.value,''); assert.equal(app.nodes.requests.textContent,'0');
 assert.equal(app.nodes.success.textContent,'无数据');
 assert.equal(app.nodes.refresh.disabled,false);
 await app.nodes.download.handlers.click();
 assert.match(app.nodes.connection.textContent,/已下载/);
 assert.ok(calls.every(([url,opts])=>url.startsWith('/lab/')&&!opts.method));
 app.nodes.disconnect.handlers.click();
 assert.equal(app.nodes.requests.textContent,'—');assert.equal(app.nodes.refresh.disabled,true);
});
test('late response after disconnect cannot repopulate snapshot',async()=>{
 let resolve;
 const app=setup(()=>new Promise(done=>{resolve=done;}));
 app.connect('good'); app.nodes.disconnect.handlers.click(); resolve(ok());
 await tick();await tick();
 assert.equal(app.nodes.requests.textContent,'—'); assert.match(app.nodes.connection.textContent,/已断开/);
});
test('newer login wins over older response',async()=>{
 const pending=[];const app=setup(()=>new Promise(done=>pending.push(done)));
 app.connect('first');app.connect('second');pending[1](ok());await tick();await tick();
 pending[0]({ok:false,status:401});await tick();await tick();
 assert.match(app.nodes.connection.textContent,/已连接/);assert.equal(app.nodes.requests.textContent,'0');
});
test('dynamic values use text nodes, no credential persistence APIs',()=>{
 const js=fs.readFileSync(path.join(__dirname,'../app/static/console.js'),'utf8');
 assert.doesNotMatch(js,/innerHTML|localStorage|sessionStorage|document\.cookie/);
});
