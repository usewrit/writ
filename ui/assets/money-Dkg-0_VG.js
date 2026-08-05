function i(r,t){const e=(typeof r=="number"&&Number.isFinite(r)?r:0)/1e6;return t!=null&&t.short?`$${e.toFixed(2)}`:e!==0&&Math.abs(e)<.01?`$${e.toFixed(4)}`:`$${e.toFixed(2)}`}export{i as f};
