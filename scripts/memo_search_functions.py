"""第22課題(検索できるDB)のメモ保存・検索を、PostgreSQL最小権限化(PR #29)の
SECURITY DEFINER関数として提供する定義。

PR #29(scripts/least_privilege_lib.py、関数一覧・GRANT・ロールバックへ追加)と
PR #30(streamlit/db.py、この2関数を呼び出す側／テスト)の両方から参照される
共有モジュール。scripts/target_identity.pyと同じく、両ブランチへバイト単位で
同一の内容を配置する(内容が完全一致すればgit上のコンフリクトは発生しない)。

record_with_memo_for_tenant()・search_records_for_tenant()とも、既存の
load_dates_for_tenant()等(least_privilege_lib.py)と同じ設計方針に従う。

- LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
  (search_path乗っ取り防止のため、テーブル参照は常にpublic.*で完全修飾する)
- pg_catalog.set_config('app.tenant_id', p_tenant_id::text, true)を呼び、
  RLSポリシー(records_tenant_isolation)が正しく世帯単位に絞り込めるようにする
- keywordのSQL LIKEワイルドカード(%・_・バックスラッシュ)は関数内でエスケープする
  (呼び出し元の実装(Python側db.py・将来の他クライアント)を信頼せず、関数自身が
  安全であることを保証する。defense in depth)
- p_orderの検証(asc/descのみ許可)も関数内で行う

実行方法(このモジュール自体はDDLを実行しない、定義を提供するだけ)は
scripts/least_privilege_lib.pyのcreate_or_replace_functions()から
呼ばれるほか、テストのセットアップからも直接呼ばれる。
public.records.memo列が事前に存在しない状態でこの関数を作成しようとすると
CREATE FUNCTION自体が失敗する(PL/pgSQL本体のコンパイル時チェックで
存在しない列への参照が検出されるため)。呼び出し元は必ず
scripts/migrate_to_records_memo_schema.pyのensure_records_memo_column()を
先に(同じトランザクション内で)実行すること。
"""

MEMO_SEARCH_FUNCTION_DEFINITIONS = [
    r"""
    CREATE OR REPLACE FUNCTION public.record_with_memo_for_tenant(
      p_tenant_id uuid, p_record_date date, p_memo text
    ) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
    AS $$
    BEGIN
      PERFORM pg_catalog.set_config('app.tenant_id', p_tenant_id::text, true);
      INSERT INTO public.records (tenant_id, record_date, memo)
      VALUES (p_tenant_id, p_record_date, p_memo)
      ON CONFLICT (tenant_id, record_date) DO UPDATE SET memo = EXCLUDED.memo;
    END;
    $$;
    """,
    r"""
    CREATE OR REPLACE FUNCTION public.search_records_for_tenant(
      p_tenant_id uuid, p_keyword text, p_order text
    ) RETURNS TABLE(record_date date, memo text)
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
    AS $$
    DECLARE
      v_escaped_keyword text;
    BEGIN
      IF p_order NOT IN ('asc', 'desc') THEN
        RAISE EXCEPTION 'p_orderは''asc''または''desc''で指定してください: %', p_order
          USING ERRCODE = 'invalid_parameter_value';
      END IF;

      PERFORM pg_catalog.set_config('app.tenant_id', p_tenant_id::text, true);

      IF p_keyword IS NOT NULL AND p_keyword <> '' THEN
        v_escaped_keyword := replace(replace(replace(p_keyword, '\', '\\'), '%', '\%'), '_', '\_');
      END IF;

      RETURN QUERY
        SELECT r.record_date, r.memo
        FROM public.records r
        WHERE r.tenant_id = p_tenant_id
          AND (v_escaped_keyword IS NULL OR r.memo ILIKE '%' || v_escaped_keyword || '%' ESCAPE '\')
        ORDER BY
          CASE WHEN p_order = 'asc' THEN r.record_date END ASC,
          CASE WHEN p_order = 'desc' THEN r.record_date END DESC;
    END;
    $$;
    """,
]

MEMO_SEARCH_FUNCTION_SIGNATURES = [
    "record_with_memo_for_tenant(uuid, date, text)",
    "search_records_for_tenant(uuid, text, text)",
]


def create_or_replace_memo_search_functions(cur):
    for definition in MEMO_SEARCH_FUNCTION_DEFINITIONS:
        cur.execute(definition)
