const fs=require('fs');
const src=fs.readFileSync('/mnt/user-data/outputs/lettercube_sim.html','utf8').split('<script>')[1].split('</script>')[0];
const core=src.slice(src.indexOf('const dv='),src.indexOf('function draw(){'));
// PRNG (mulberry32) ที่ seed ได้ — ใช้แทน Math.random ใน core
function H(s){let h=2166136261;for(const c of String(s)){h^=c.charCodeAt(0);h=Math.imul(h,16777619)}return h>>>0}
function mkInst(det,seed){
  let st=H(seed);const rnd=()=>{st=(st+0x6D2B79F5)|0;let t=Math.imul(st^st>>>15,1|st);t=t+Math.imul(t^t>>>7,61|t)^t;return((t^t>>>14)>>>0)/4294967296};
  let c=core;
  if(det){ // เวอร์ชัน deterministic: letter ของแถว/ก้อนคำนวณจาก (seed, ตำแหน่ง) ไม่ขึ้นกับลำดับสร้าง
    c=c.replace('if(!rows.has(key))rows.set(key,','if(!rows.has(key)){seedR("r"+key)}if(!rows.has(key))rows.set(key,');
    c=c.replace('const pool=shuf(LET.filter','seedR("c"+pos.join());const pool=shuf(LET.filter');
  }
  const f=new Function('Math','seedR',c+';return{gen,links,rows,reset,get cubes(){return cubes},set cubes(v){cubes=v},get mode(){return mode}}');
  const M=Object.create(Math);M.random=rnd;
  return f(M,s=>{st=H(seed+"|"+s)});
}
const grid=(nx,ny,nz)=>{const a=[];for(let z=0;z<nz;z++)for(let y=0;y<ny;y++)for(let x=0;x<nx;x++)a.push([x,y,z]);return a};
const shuffleArr=(a,r)=>{a=a.slice();for(let i=a.length-1;i>0;i--){const j=Math.floor(r()*(i+1));[a[i],a[j]]=[a[j],a[i]]}return a};
const lcg=(s)=>()=>((s=(s*1664525+1013904223)>>>0)/4294967296);
const sig=c=>JSON.stringify([c.L,c.S]);
function build(inst,order){inst.rows.clear();const cs=order.map(p=>inst.gen(p));inst.cubes=cs;return cs}
// ---------- TEST 1: order independence (1000 ก้อน, 3 ลำดับ) ----------
console.log('TEST 1  order independence, 10x10x10 = 1000 cubes');
const G=grid(10,10,10),orders={snake:G,reverse:G.slice().reverse(),random:shuffleArr(G,lcg(5))};
for(const det of[false,true]){
  const inst=mkInst(det,"S1"),res={};
  for(const[n,o]of Object.entries(orders)){
    // ลำดับสร้างต่างกัน; reset PRNG stream สำหรับตัวไม่ deterministic ให้เหมือนกัน (เพื่อแยกผลของ "ลำดับ")
    const i2=mkInst(det,"S1");const cs=build(i2,o);const L=i2.links();
    const m=new Map(cs.map(c=>[c.pos.join(),sig(c)]));res[n]={m,ok:L.ok,tot:L.tot};
  }
  const base=res.snake.m;let diff={};
  for(const n of['reverse','random']){let d=0;for(const[k,v]of base)if(res[n].m.get(k)!==v)d++;diff[n]=d}
  console.log(' ',det?'deterministic (seed+row key)':'current (RNG ตามลำดับสร้าง)',
   '| junction',Object.entries(res).map(([n,r])=>n+' '+r.ok+'/'+r.tot).join(', '),
   '| ก้อนที่ letter ต่างจาก snake:',JSON.stringify(diff));
}
// ---------- TEST 2: loop closure ----------
console.log('TEST 2  loop closure (random walk เดินทุกทิศ ไม่ซ้ำช่อง)');
for(const det of[false,true]){
  let tot=0,ok=0,nonConsec=0,walks=40;
  for(let w=0;w<walks;w++){
    const inst=mkInst(det,"W"+w),r=lcg(100+w);inst.rows.clear();
    const seen=new Set(["0,0,0"]);let p=[0,0,0];const path=[p.slice()];
    for(let s=0;s<800;s++){const opts=[];for(let k=0;k<3;k++)for(const d of[-1,1]){const q=p.slice();q[k]+=d;if(Math.max(...q.map(Math.abs))<=4&&!seen.has(q.join()))opts.push(q)}
      if(!opts.length)break;p=opts[Math.floor(r()*opts.length)];seen.add(p.join());path.push(p.slice())}
    inst.cubes=path.map(q=>inst.gen(q));const L=inst.links();ok+=L.ok;tot+=L.tot;
    // นับคู่ติดกันที่ "ไม่ใช่ก้อนต่อเนื่อง" = ลูปปิด
    const idx=new Map(path.map((q,i)=>[q.join(),i]));
    path.forEach((q,i)=>{for(let k=0;k<3;k++){const n=q.slice();n[k]++;const j=idx.get(n.join());if(j!==undefined&&Math.abs(j-i)!==1)nonConsec++}});
  }
  console.log(' ',det?'deterministic':'current','| junction',ok+'/'+tot,'| คู่ติดกันที่ปิดลูป (ไม่ต่อเนื่อง):',nonConsec,'จาก',walks,'เส้นทาง');
}
// ---------- TEST 3: regrow persistence ----------
console.log('TEST 3  regrow persistence (สร้าง 512 → ปิด → สร้างต่ออีก 512)');
const A=grid(8,8,8),B=grid(8,8,16).slice(512);
const rowSig=c=>JSON.stringify([c.L.slice(12),c.S]);
// 3a: ไม่มี log, RNG ใหม่ (ตัวควบคุมเชิงลบ)
{const i1=mkInst(false,"R1");const old=build(i1,A);
 const i2=mkInst(false,"R2");i2.rows.clear();i2.cubes=old.concat(B.map(p=>i2.gen(p)));const L=i2.links();
 console.log('  3a current ไม่มี log     | junction',L.ok+'/'+L.tot,'(ควรเสีย เพราะแถวใหม่ไม่รู้แถวเก่า)')}
// 3b: allocation log (เก็บ rows ที่จัดสรรแล้ว) แล้วโหลดกลับ
{const i1=mkInst(false,"R1");const old=build(i1,A);
 const log=JSON.stringify([...i1.rows]);                       // allocation log
 const i2=mkInst(false,"R2");i2.rows.clear();for(const[k,v]of JSON.parse(log))i2.rows.set(k,v);
 const nw=B.map(p=>i2.gen(p));i2.cubes=old.concat(nw);const L=i2.links();
 // แถวเดิมไม่เปลี่ยน: gen ใหม่ที่ตำแหน่งเก่า ต้องได้ letter แถวเดิมเหมือนเดิม
 let same=0;for(const c of old){if(rowSig(i2.gen(c.pos))===rowSig(c))same++}
 console.log('  3b current + log (rows)  | junction',L.ok+'/'+L.tot,'| แถวเดิมไม่เปลี่ยน',same+'/'+old.length,'| inner ของก้อนเดิมต้องเก็บแยก (ไม่อยู่ใน log แถว)')}
// 3c: deterministic ไม่ต้องมี log
{const i1=mkInst(true,"R1");const old=build(i1,A);
 const i2=mkInst(true,"R1");i2.rows.clear();const nw=B.map(p=>i2.gen(p));i2.cubes=old.concat(nw);const L=i2.links();
 let same=0;for(const c of old){if(sig(i2.gen(c.pos))===sig(c))same++}
 console.log('  3c deterministic ไม่มี log| junction',L.ok+'/'+L.tot,'| ก้อนเดิมเหมือนเดิมทั้ง letter+inner',same+'/'+old.length)}
// ---------- signature ซ้ำ (ข้อจำกัด alphabet) ----------
{const i=mkInst(true,"D");i.rows.clear();build(i,grid(10,10,10));
 for(let k=0;k<3;k++){const s=[...i.rows].filter(([key])=>key[0]==k).map(([,v])=>JSON.stringify(v));console.log(' แกน',k,'แถว',s.length,'signature ไม่ซ้ำ',new Set(s).size)}}
