"""Rebuild Markdown projections from authoritative SQLite records."""
from . import storage


def main():
    for space in storage.SPACES:
        storage.export_memories(space)
        for session in storage.list_sessions(space):
            storage.export_memories(space,session['id'])
        print(f'{space}: Markdown 已同步')


if __name__=='__main__':
    main()
