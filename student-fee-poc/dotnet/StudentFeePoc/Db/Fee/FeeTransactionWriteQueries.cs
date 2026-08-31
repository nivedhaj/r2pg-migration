using System.Text.Json;
using Npgsql;

namespace StudentFeePoc.Db.Fee;

public sealed record CreateFeeTransactionRequest(
    Guid? StudentId,
    decimal Amount,
    string? Status,
    Guid? FeeId,
    string? RefNo = null,
    string? PaidBy = "Cash"
);

public sealed record CreateFeeTransactionResponse(
    Guid Id,
    string TxNo,
    DateTime TxDate,
    Guid? StudentId,
    decimal Amount,
    string Status,
    string Message
);

public static class FeeTransactionWriteQueries
{
    public static async Task<CreateFeeTransactionResponse> InsertFeeTransactionAsync(
        NpgsqlDataSource dataSource,
        CreateFeeTransactionRequest request)
    {
        var id = Guid.NewGuid();
        var txNo = $"TXN-{DateTime.UtcNow:yyyyMMddHHmmssfff}";
        var txDate = DateTime.UtcNow;
        var status = request.Status ?? "Active";

        var installmentsPaid = new[]
        {
            new
            {
                FeeId = request.FeeId,
                Amount = request.Amount,
                DueDate = DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ss")
            }
        };

        var installmentsPaidJson = JsonSerializer.Serialize(installmentsPaid);

        const string sql = """
            INSERT INTO fee_transaction (
                id,
                tx_no,
                tx_date,
                student_id,
                amount,
                status,
                ref_no,
                paid_by,
                installments_paid,
                fines_paid,
                discounts,
                fee_adjustment,
                is_fine_paid,
                is_discount_given,
                is_opening_balance_adjusted
            ) VALUES (
                @id,
                @tx_no,
                @tx_date,
                @student_id,
                @amount,
                @status::fee_tx_status_enum,
                @ref_no,
                @paid_by,
                @installments_paid::jsonb,
                '[]'::jsonb,
                '[]'::jsonb,
                '{}'::jsonb,
                false,
                false,
                false
            );
            """;

        await using var command = dataSource.CreateCommand(sql);
        command.Parameters.AddWithValue("id", id);
        command.Parameters.AddWithValue("tx_no", txNo);
        command.Parameters.AddWithValue("tx_date", txDate);
        command.Parameters.AddWithValue("student_id", (object?)request.StudentId ?? DBNull.Value);
        command.Parameters.AddWithValue("amount", request.Amount);
        command.Parameters.AddWithValue("status", status);
        command.Parameters.AddWithValue("ref_no", (object?)request.RefNo ?? DBNull.Value);
        command.Parameters.AddWithValue("paid_by", (object?)request.PaidBy ?? DBNull.Value);
        command.Parameters.AddWithValue("installments_paid", installmentsPaidJson);

        await command.ExecuteNonQueryAsync();

        return new CreateFeeTransactionResponse(
            id,
            txNo,
            txDate,
            request.StudentId,
            request.Amount,
            status,
            "Fee transaction inserted successfully."
        );
    }
}
