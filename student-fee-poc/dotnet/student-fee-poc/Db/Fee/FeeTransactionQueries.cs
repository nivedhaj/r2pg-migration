using System.Text.Json;
using Npgsql;
using StudentFeePoc.Models;

namespace StudentFeePoc.Db.Fee;

public static class FeeTransactionQueries
{
    public static async Task<FeeTransactionApiResponse> GetFeeTransactionsAsync(
        NpgsqlDataSource dataSource,
        Guid? id = null,
        int limit = 10)
    {
        // 1. Fetch fee lookup map for feeDisplayText & installment descriptions
        const string feeLookupSql = """
            SELECT id, name, display_text, installments
            FROM fee;
            """;

        var feeLookup = new Dictionary<Guid, (string? Name, string? DisplayText, JsonElement Installments)>();
        await using (var feeCmd = dataSource.CreateCommand(feeLookupSql))
        await using (var feeReader = await feeCmd.ExecuteReaderAsync())
        {
            while (await feeReader.ReadAsync())
            {
                var fId = feeReader.GetGuid(0);
                var fName = feeReader.IsDBNull(1) ? null : feeReader.GetString(1);
                var fDisplay = feeReader.IsDBNull(2) ? null : feeReader.GetString(2);
                var installmentsJson = feeReader.IsDBNull(3)
                    ? default
                    : JsonDocument.Parse(feeReader.GetString(3)).RootElement;

                feeLookup[fId] = (fName, fDisplay, installmentsJson);
            }
        }

        // 2. Fetch fee transactions joined with student
        const string txSql = """
            SELECT 
                ft.id,
                ft.owner_id,
                COALESCE(ft.parent_id, ft.owner_id) AS parent_id,
                ft.tx_no,
                to_char(ft.tx_date AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS') AS tx_date,
                ft.student_id,
                s.name AS student_name,
                COALESCE(se.enrollment->>'CourseId', '') AS course_id,
                COALESCE(se.enrollment->>'CourseName', '') AS course_name,
                COALESCE(se.enrollment->>'SectionName', '') AS section,
                ft.amount,
                COALESCE(ft.installments_paid, '[]'::jsonb)::text AS installments_paid_json,
                COALESCE(ft.fines_paid, '[]'::jsonb)::text AS fines_paid_json,
                COALESCE(ft.discounts, '[]'::jsonb)::text AS discounts_json,
                COALESCE(ft.fee_adjustment, '{}'::jsonb)::text AS fee_adjustment_json,
                COALESCE(ft.is_fine_paid, false) AS is_fine_paid,
                COALESCE(ft.is_discount_given, false) AS is_discount_given,
                COALESCE(ft.is_opening_balance_adjusted, false) AS is_opening_balance_adjusted,
                CASE 
                    WHEN ft.status = 'Disabled' THEN 99
                    WHEN ft.status = 'Active' THEN 1
                    ELSE 0
                END AS status_code,
                ft.ref_no,
                ft.paid_by
            FROM fee_transaction ft
            LEFT JOIN student s ON s.id = ft.student_id
            LEFT JOIN LATERAL (
                SELECT enrollment 
                FROM jsonb_array_elements(
                    CASE WHEN jsonb_typeof(s.enrollments) = 'array' THEN s.enrollments ELSE '[]'::jsonb END
                ) AS enrollment 
                LIMIT 1
            ) se ON TRUE
            WHERE (@filterId::uuid IS NULL OR ft.id = @filterId::uuid)
            ORDER BY ft.tx_date DESC NULLS LAST, ft.tx_no DESC
            LIMIT @limit;
            """;

        var dtoList = new List<FeeTransactionDto>();
        await using var command = dataSource.CreateCommand(txSql);
        command.Parameters.AddWithValue("filterId", (object?)id ?? DBNull.Value);
        command.Parameters.AddWithValue("limit", limit);

        await using var reader = await command.ExecuteReaderAsync();
        while (await reader.ReadAsync())
        {
            var txId = reader.GetGuid(0);
            var ownerId = reader.IsDBNull(1) ? (Guid?)null : reader.GetGuid(1);
            var parentId = reader.IsDBNull(2) ? (Guid?)null : reader.GetGuid(2);
            var txNo = reader.IsDBNull(3) ? null : reader.GetString(3);
            var txDate = reader.IsDBNull(4) ? null : reader.GetString(4);
            var studentId = reader.IsDBNull(5) ? (Guid?)null : reader.GetGuid(5);
            var studentName = reader.IsDBNull(6) ? null : reader.GetString(6);
            var courseId = reader.IsDBNull(7) ? null : reader.GetString(7);
            var courseName = reader.IsDBNull(8) ? null : reader.GetString(8);
            var section = reader.IsDBNull(9) ? null : reader.GetString(9);
            var amount = reader.GetDecimal(10);
            var instPaidJson = reader.GetString(11);
            var finesPaidJson = reader.GetString(12);
            var discountsJson = reader.GetString(13);
            var feeAdjJson = reader.GetString(14);
            var isFinePaid = reader.GetBoolean(15);
            var isDiscountGiven = reader.GetBoolean(16);
            var isOpeningBalanceAdjusted = reader.GetBoolean(17);
            var status = reader.GetInt32(18);
            var refNo = reader.IsDBNull(19) ? null : reader.GetString(19);
            var paidBy = reader.IsDBNull(20) ? null : reader.GetString(20);

            // Parse installments paid
            var installmentsList = new List<InstallmentPaidDto>();
            using (var doc = JsonDocument.Parse(instPaidJson))
            {
                if (doc.RootElement.ValueKind == JsonValueKind.Array)
                {
                    foreach (var elem in doc.RootElement.EnumerateArray())
                    {
                        Guid? fId = null;
                        if (elem.TryGetProperty("FeeId", out var fProp) || elem.TryGetProperty("feeId", out fProp))
                        {
                            if (fProp.TryGetGuid(out var parsedGuid))
                                fId = parsedGuid;
                            else if (Guid.TryParse(fProp.GetString(), out var g))
                                fId = g;
                        }

                        feeLookup.TryGetValue(fId ?? Guid.Empty, out var fInfo);

                        var fName = elem.TryGetProperty("FeeName", out var fnProp) ? fnProp.GetString() : fInfo.Name;
                        var fDisplay = elem.TryGetProperty("FeeDisplayText", out var fdProp) && !string.IsNullOrEmpty(fdProp.GetString())
                            ? fdProp.GetString()
                            : fInfo.DisplayText;

                        int? instId = null;
                        if (elem.TryGetProperty("InstallmentId", out var instProp) && instProp.TryGetInt32(out var parsedInstId))
                            instId = parsedInstId;
                        else if (elem.TryGetProperty("Id", out var idProp) && idProp.TryGetInt32(out var parsedId))
                            instId = parsedId;

                        string? desc = null;
                        if (elem.TryGetProperty("Description", out var descProp) && !string.IsNullOrEmpty(descProp.GetString()))
                            desc = descProp.GetString();
                        else if (fInfo.Installments.ValueKind == JsonValueKind.Array)
                        {
                            foreach (var fi in fInfo.Installments.EnumerateArray())
                            {
                                if (fi.TryGetProperty("Id", out var fiId) && fiId.TryGetInt32(out var fiIdVal) && fiIdVal == instId)
                                {
                                    if (fi.TryGetProperty("Description", out var fiDesc))
                                        desc = fiDesc.GetString();
                                    break;
                                }
                            }
                        }

                        string? dueDate = null;
                        if (elem.TryGetProperty("DueDate", out var ddProp))
                        {
                            var ddStr = ddProp.GetString();
                            if (ddStr != null && ddStr.Contains('.'))
                                dueDate = ddStr.Split('.')[0];
                            else
                                dueDate = ddStr;
                        }

                        decimal instAmount = 0m;
                        if (elem.TryGetProperty("Amount", out var amProp) && amProp.TryGetDecimal(out var parsedAmount))
                            instAmount = parsedAmount;

                        installmentsList.Add(new InstallmentPaidDto(
                            fId,
                            fName,
                            fDisplay,
                            instId,
                            desc,
                            dueDate ?? "2015-08-20T00:00:00",
                            instAmount
                        ));
                    }
                }
            }

            // Parse fee adjustment
            var adjId = Guid.Empty;
            var adjName = "Adj Fee";
            var adjDesc = "Extra Amount";
            var adjAmount = 0m;
            using (var doc = JsonDocument.Parse(feeAdjJson))
            {
                if (doc.RootElement.ValueKind == JsonValueKind.Object)
                {
                    if (doc.RootElement.TryGetProperty("Id", out var idp) && idp.TryGetGuid(out var gid))
                        adjId = gid;
                    if (doc.RootElement.TryGetProperty("Name", out var np))
                        adjName = np.GetString() ?? adjName;
                    if (doc.RootElement.TryGetProperty("Description", out var dp))
                        adjDesc = dp.GetString() ?? adjDesc;
                    if (doc.RootElement.TryGetProperty("Amount", out var ap) && ap.TryGetDecimal(out var aVal))
                        adjAmount = aVal;
                }
            }

            var feeAdjustment = new FeeAdjustmentDto(adjId, adjName, adjDesc, adjAmount);
            var finesPaid = JsonSerializer.Deserialize<List<object>>(finesPaidJson) ?? new List<object>();
            var discounts = JsonSerializer.Deserialize<List<object>>(discountsJson) ?? new List<object>();

            dtoList.Add(new FeeTransactionDto(
                txId,
                ownerId,
                parentId,
                txNo,
                txDate,
                studentId,
                studentName,
                courseId,
                courseName,
                section,
                amount,
                installmentsList,
                finesPaid,
                discounts,
                feeAdjustment,
                isFinePaid,
                isDiscountGiven,
                isOpeningBalanceAdjusted,
                status,
                refNo,
                paidBy
            ));
        }

        return new FeeTransactionApiResponse(dtoList);
    }
}
