"""CLI for LC_tilecube Read Path.

Usage:
  python read_path_cli.py <storage_dir> list <branch>
  python read_path_cli.py <storage_dir> read <branch> <index>
  python read_path_cli.py <storage_dir> reconstruct <item_id>
  python read_path_cli.py <storage_dir> search <branch> <item_id_or_vecfile> [top_k]
  python read_path_cli.py <storage_dir> stats
"""
import sys, os, json, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from read_path import ReadPath


def cmd_list(rp, args):
    addrs = rp.list_addresses(args.branch)
    print(f"branch {args.branch}: {len(addrs)} addresses")
    for a in addrs:
        info = rp.read(a)
        print(f"  {a.pos} items={info['item_ids']}")


def cmd_read(rp, args):
    info = rp.read((args.branch, args.index))
    print(json.dumps(info, indent=2, ensure_ascii=False))


def cmd_reconstruct(rp, args):
    rec = rp.reconstruct(args.item_id)
    if rec is None:
        print("not found")
        return
    print(json.dumps(rec, indent=2, ensure_ascii=False))


def cmd_search(rp, args):
    top_k = args.top_k
    q = args.query
    # if query is a file, load vector
    if os.path.exists(q):
        with open(q) as f:
            v = json.load(f)
        query = v
    else:
        query = q  # item_id
    res = rp.search(args.branch, query, top_k=top_k)
    for r in res:
        print(f"{r['key']}  score={r['score']:.4f}")


def cmd_stats(rp, args):
    print(f"branches: {rp._branch_count()}")
    for b in range(rp._branch_count()):
        print(f"  branch {b}: {len(rp.list_addresses(b))} addresses")
    print(f"relations: {len(rp.relations)}")
    if rp.fruit_store is not None:
        print(f"fruits: {list(rp.fruit_store.fruits.keys())}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("storage_dir")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("list"); s.add_argument("branch", type=int)
    s = sub.add_parser("read"); s.add_argument("branch", type=int); s.add_argument("index", type=int)
    s = sub.add_parser("reconstruct"); s.add_argument("item_id")
    s = sub.add_parser("search"); s.add_argument("branch", type=int); s.add_argument("query"); s.add_argument("top_k", nargs="?", type=int, default=5)
    s = sub.add_parser("stats")
    args = p.parse_args()
    rp = ReadPath(args.storage_dir)
    {"list": cmd_list, "read": cmd_read, "reconstruct": cmd_reconstruct,
     "search": cmd_search, "stats": cmd_stats}[args.cmd](rp, args)


if __name__ == "__main__":
    main()
