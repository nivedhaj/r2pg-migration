using Npgsql;

namespace StudentFeePoc.Db.Fee;

public sealed record FeeItemResponse(
    Guid Id,
    Guid? InstId,
    string? Name,
    string? DisplayText,
    string? Status,
    bool IsTxDone
);

public sealed record FeeListApiResponse(
    IReadOnlyList<FeeItemResponse> Data
);

public static class FeeQueries
{
    public static async Task<FeeListApiResponse> GetFeesAsync(
        NpgsqlDataSource dataSource,
        int limit = 50)
    {
        const string sql = """
            SELECT
                f.id,
                COALESCE(f.owner_id, f.parent_id) AS inst_id,
                f.name,
                COALESCE(f.display_text, f.name) AS display_text,
                f.status,
                EXISTS (
                    SELECT 1 FROM fee_transaction ft 
                    WHERE EXISTS (
                        SELECT 1 FROM jsonb_array_elements(
                            CASE WHEN jsonb_typeof(ft.installments_paid) = 'array' THEN ft.installments_paid ELSE '[]'::jsonb END
                        ) inst 
                        WHERE (inst->>'feeId')::text = f.id::text OR (inst->>'FeeId')::text = f.id::text
                    )
                ) AS is_tx_done
            FROM fee f
            ORDER BY f.name NULLS LAST, f.id
            LIMIT @limit;
            """;

        var fees = new List<FeeItemResponse>();
        await using var command = dataSource.CreateCommand(sql);
        command.Parameters.AddWithValue("limit", limit);

        await using var reader = await command.ExecuteReaderAsync();
        while (await reader.ReadAsync())
        {
            fees.Add(new FeeItemResponse(
                reader.GetGuid(0),
                reader.IsDBNull(1) ? null : reader.GetGuid(1),
                reader.IsDBNull(2) ? null : reader.GetString(2),
                reader.IsDBNull(3) ? null : reader.GetString(3),
                reader.IsDBNull(4) ? null : reader.GetString(4),
                reader.GetBoolean(5)
            ));
        }

        return new FeeListApiResponse(fees);
    }
}
