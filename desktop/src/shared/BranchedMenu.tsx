// React Bits BranchedMenu (JS/CSS): adapted tree/fold/draw mechanism; see THIRD-PARTY-MOTION.md.
import type { CSSProperties, ReactNode } from 'react';
import './BranchedMenu.css';

export interface BranchedMenuItem {
  value: string;
  content: ReactNode;
  heading?: boolean;
}

/** The existing navigation owns the section button; the backend owns the active value. */
export function BranchedMenu({ items, activeValue, open, footer, id }: {
  items: BranchedMenuItem[]; activeValue?: string; open: boolean; footer?: ReactNode; id: string;
}) {
  const pad = 6, rowHeight = 44, indent = 40, trunk = 14, radius = 10;
  let height = pad;
  const rows = items.map(item => {
    const size = item.heading ? 28 : rowHeight;
    const row = { ...item, top: height, y: height + size / 2, size };
    height += size;
    return row;
  });
  const branches = rows.filter(row => !row.heading);
  const branch = (y: number) => `M ${trunk} ${y - radius} A ${radius} ${radius} 0 0 0 ${trunk + radius} ${y} H ${indent - 8}`;
  const reach = (y: number) => `M ${trunk} 0 V ${y - radius} A ${radius} ${radius} 0 0 0 ${trunk + radius} ${y} H ${indent - 8}`;
  const length = (y: number) => y - radius + Math.PI * radius / 2 + indent - 8 - trunk - radius;
  return <div id={id} className="branched-menu" data-open={open ? '' : undefined} inert={!open}
    role="group" aria-label="Histórico de conversas" style={{ '--bm-indent': `${indent}px` } as CSSProperties}>
    <div className="branched-menu__fold">
      <div className="branched-menu__scroll" tabIndex={open ? 0 : -1} aria-label="Lista de conversas">
        <div className="branched-menu__tree" style={{ height: height + pad }}>
          <svg className="branched-menu__lines" width={indent} height={height + pad} aria-hidden="true">
            {branches.length > 0 && <path className="branched-menu__base" d={`M ${trunk} 0 V ${branches.at(-1)!.y - radius}`} />}
            {branches.map(row => <path key={row.value} className="branched-menu__base" d={branch(row.y)} />)}
            {branches.map(row => <path key={row.value} className="branched-menu__reach" d={reach(row.y)}
              style={{ strokeDasharray: length(row.y), strokeDashoffset: row.value === activeValue ? 0 : length(row.y) }} />)}
          </svg>
          {rows.map(row => <div key={row.value} className={row.heading ? 'branched-menu__heading' : 'branched-menu__row'}
            style={{ top: row.top, height: row.size }}>{row.content}</div>)}
        </div>
        {footer}
      </div>
    </div>
  </div>;
}
