地方競馬 複勝投票管理 v1

Renderで別サービスとして使う場合:
Build Command: pip install -r requirements.txt
Start Command: gunicorn app:app

推奨Environment Variables:
MEMBER_ID=任意の会員ID
MEMBER_PASSWORD=任意のパスワード
FLASK_SECRET_KEY=Renderで生成した十分長いランダム文字列

重要:
ワイド版とは別サービス/別URLで運用してください。
このv1は市場オッズ中心のルールベース参考評価で、的中・利益を保証しません。
本番販売前には永続DB(PostgreSQL等)への移行を推奨します。
