using Npgsql;

namespace StudentFeePoc.Db.Fee;

public sealed record FeeRow(
    Guid Id,
    string? Name,
    decimal? Amount,
    string? Status
);

public static class FeeQueries
{
    public static async Task<IReadOnlyList<FeeRow>> GetRecentFeesAsync(
        NpgsqlDataSource dataSource,
        int limit = 10)
    {
        const string sql = """
            SELECT id, name, amount, status
            FROM fee
            ORDER BY name NULLS LAST, id
            LIMIT @limit;
            """;

        var fees = new List<FeeRow>();
        await using var command = dataSource.CreateCommand(sql);
        command.Parameters.AddWithValue("limit", limit);

        await using var reader = await command.ExecuteReaderAsync();
        while (await reader.ReadAsync())
        {
            fees.Add(new FeeRow(
                reader.GetGuid(0),
                reader.IsDBNull(1) ? null : reader.GetString(1),
                reader.IsDBNull(2) ? null : reader.GetDecimal(2),
                reader.IsDBNull(3) ? null : reader.GetString(3)
            ));
        }

        return fees;
    }
}
