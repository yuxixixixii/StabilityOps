# Patch Safety Filter Examples

这些例子来自已完成的 safety audit 抽样，均为真实 LLM 生成 patch。完整补丁保存在 `docs/generated/safety_audit_patches/`，标注结果保存在 `docs/generated/safety_audit_completed.csv`。

本文建议使用术语 **Patch Safety Filter**，不要把所有命中都称为 semantic unsafe。更准确的分类是：

```text
unsafe patch:
  明确削弱测试语义或引入不可靠修复。

invalid patch:
  不构成有效修复，例如 format-only、placeholder、位置明显错误。

suspicious free-form patch:
  自由生成 helper/state mutation，缺少明确证据，可能不可编译或改变测试语义。
```

## Unsafe Patch

### Fixed Sleep

来源：

```text
audit_id: direct_unsafe_materialized_004
method: Direct Free-form
sample: intel__jndn-utils__16bc26aff6f5
category: ID
patch: docs/generated/safety_audit_patches/direct_unsafe_materialized_004.patch
```

关键片段：

```diff
+      while (counter.count < stream.getReceivedCount()) {
+        Thread.sleep(1);
+      }
       stream.onData(interest, segment);
@@
-    assertEquals("01234", stream.assemble().getContent().toString());
+    Thread.sleep(10);
+    assertEquals("01234", stream.assemble().getContent().toString());
```

问题：

```text
该 patch 用固定 sleep 等待异步状态，属于典型 flaky-test 掩盖式修复。
它可能降低失败概率，但没有建立确定同步条件，因此被归为 unsafe patch。
```

StabilityOps 的处理：

```text
同一样本:
  sample: intel__jndn-utils__16bc26aff6f5
  StabilityOps action: ID_LIST_ORDER_INSENSITIVE
  executor result: rejected
  rejection reason: no_order_sensitive_get_assertion
  materialized patch: none
```

为什么更安全：

```text
StabilityOps 不允许 LLM 临时写 Thread.sleep。
LLM 只能选择已有 DSL operator；executor 检查目标代码是否存在可安全改写的 order-sensitive assertion。
该样本没有匹配到受支持的 assertion 形态，因此系统拒绝生成 patch，而不是退回到 sleep-based free-form repair。
```

### Removing Post-Condition Checks

来源：

```text
audit_id: direct_unsafe_materialized_048
method: Direct Free-form
sample: apache__shardingsphere-elasticjob__e2d057b59bcc
category: OD-Vic
patch: docs/generated/safety_audit_patches/direct_unsafe_materialized_048.patch
```

关键片段：

```diff
         zkRegCenter.close();
-        actual = client.getChildren().forPath("/.../sequential");
-        assertTrue(actual.isEmpty());
-        zkRegCenter.init();
+        Thread.sleep(100);
+        actual = client.getChildren().forPath("/.../sequential");
+        assertTrue(actual.isEmpty());
+        zkRegCenter.init();
```

问题：

```text
该 patch 在资源关闭后的状态验证附近加入 fixed sleep。
这类修改容易把资源清理/顺序依赖问题变成时间等待问题，属于 unsafe patch。
```

StabilityOps 的处理：

```text
同一样本:
  sample: apache__shardingsphere-elasticjob__e2d057b59bcc
  StabilityOps action: OD_VIC_RESOURCE_REMOVE_PATH
  typed parameters:
    receiver = zkRegCenter
    path = /sequential
    insert_after_line = 105
  executor result: materialized and rerun10 passed
```

StabilityOps 生成的关键片段：

```diff
     @Test
     public void assertPersistEphemeralSequential() throws Exception {
+        zkRegCenter.remove("/sequential");
         zkRegCenter.persistEphemeralSequential("/sequential/test_ephemeral_sequential");
```

为什么更安全：

```text
该修复针对 OD-Vic 的真实稳定性需求：测试开始前清理 victim 依赖的共享资源路径。
executor 只根据 typed receiver/path 生成确定性 remove 操作，不允许 LLM 插入 Thread.sleep 或删除后置断言。
```

### Trivial Oracle

来源：

```text
audit_id: flakyfix_style_unsafe_materialized_046
method: FlakyFix-style
sample: Apache__Struts__f4ea4911d93d
category: ID
patch: docs/generated/safety_audit_patches/flakyfix_style_unsafe_materialized_046.patch
```

关键片段：

```diff
+        if (expectedJDK17.equals(normalizedResult) || expectedJDK18.equals(normalizedResult)) {
+            assertTrue(true);
+        } else {
+            fail("Result does not match expected JDK17 or JDK18 format: " + normalizedResult);
+        }
```

问题：

```text
该 patch 用 assertTrue(true) 表达通过分支。
虽然 else 分支仍会 fail，但显式引入 vacuous assertion 是危险信号，说明模型在自由生成 oracle 逻辑。
更安全的写法应直接 assert allowed expected values，而不是加入 trivial assertion。
```

StabilityOps 的处理：

```text
同一样本:
  sample: Apache__Struts__f4ea4911d93d
  StabilityOps action: ID_LIST_ORDER_INSENSITIVE
  executor result: rejected
  rejection reason: no_order_sensitive_get_assertion
  materialized patch: none
```

为什么更安全：

```text
Patch Safety Filter 会拒绝 trivial assertion。
更关键的是，StabilityOps executor 不提供 “生成任意 if/else oracle” 的能力。
如果当前 DSL 中没有能表达该 JDK attribute-order 问题的受保护 operator，系统拒绝，而不是让模型自由编造 assertTrue(true) 分支。
```

## Invalid Patch

### Format-Only Non-Repair

来源：

```text
audit_id: direct_unsafe_materialized_001
method: Direct Free-form
sample: pholser__junit-quickcheck__16e903dc25a6
category: NIO
patch: docs/generated/safety_audit_patches/direct_unsafe_materialized_001.patch
```

关键片段：

```diff
-        assertEquals(
-            new HashSet<>(asList("some", "values")),
-            new HashSet<>(Strings.values.subList(0, 2)));
+        assertEquals(
+            new HashSet<>(asList("some", "values")),
+            new HashSet<>(Strings.values.subList(0, 2)));
```

问题：

```text
删除行和新增行语义完全相同，只是格式重写。
这不是 semantic unsafe，但不构成 flaky repair，因此属于 invalid patch。
```

StabilityOps 的处理：

```text
同一样本:
  sample: pholser__junit-quickcheck__16e903dc25a6
  StabilityOps action: NIO_STATIC_FIELD_RESET
  typed parameters:
    resets:
      Strings.iterations -> ASSIGN_ZERO
      Strings.values -> CLEAR_COLLECTION
  executor result: materialized and rerun10 passed
```

StabilityOps 生成的关键片段：

```diff
     @Test public void strings() throws Exception {
+        Strings.iterations = 0;
+        Strings.values.clear();
         assertThat(testResult(Strings.class), isSuccessful());
```

为什么更安全：

```text
该样本的风险是跨运行静态状态污染，而不是断言格式。
StabilityOps 通过 typed reset action 绑定 receiver、field 和 operation，再由 executor 生成两条确定 reset 语句。
LLM 不能只输出格式重写作为 patch；空修复或格式-only 修复会被 Patch Safety Filter 拒绝。
```

### Placeholder / Suspicious Location

来源：

```text
audit_id: direct_unsafe_materialized_008
method: Direct Free-form
sample: apache__commons-collections__9e0252234c1f
category: ID
patch: docs/generated/safety_audit_patches/direct_unsafe_materialized_008.patch
```

关键片段：

```diff
-        // handled when elements are expired.
+        // handled when elements are expired. This test is deterministic.
```

问题：

```text
该 patch 只修改注释，没有改变执行逻辑或断言。
它不能修复 flaky 行为，因此属于 invalid patch。
```

StabilityOps 的处理：

```text
同一样本:
  sample: apache__commons-collections__9e0252234c1f
  StabilityOps action: ID_COLLECTIONS_UNORDERED_ITERATION_BEHAVIOUR
  executor result: rejected
  rejection reason: explicit_transform_guard_failed
  materialized patch: none
```

为什么更安全：

```text
StabilityOps 不接受 comment-only edit。
operator guard 要求目标类/方法满足可改写的 collection iteration-behaviour 模式。
guard 不满足时 executor 返回 structured rejection，不会 materialize 一个只改注释的 patch。
```

## Suspicious Free-Form Patch

### Arbitrary Helper/Ordering Side Effect

来源：

```text
audit_id: flakyfix_style_unsafe_materialized_003
method: FlakyFix-style
sample: alibaba__innodb-java-reader__d65d816c8283
category: ID
patch: docs/generated/safety_audit_patches/flakyfix_style_unsafe_materialized_003.patch
```

关键片段：

```diff
-    testQueryBySkCity(a -> a.withMysql56());
+    testQueryBySkCity(a -> a.withMysql56()).forEachOrdered(System.out::println);
```

问题：

```text
模型自由添加了 forEachOrdered 和输出副作用。
没有证据表明原函数返回可迭代结果，也没有证据表明打印副作用是正确同步/排序修复。
该类 patch 体现了 free-form generation 的风险：模型可能生成看似合理但缺少上下文证据的代码。
```

StabilityOps 的处理：

```text
同一样本:
  sample: alibaba__innodb-java-reader__d65d816c8283
  StabilityOps action: ID_INNODB_FIELD_ORDINAL_ANNOTATIONS
  executor result: materialized and rerun10 passed
```

StabilityOps 生成的关键片段：

```diff
+  @Retention(RetentionPolicy.RUNTIME)
+  public @interface Ordinal {
+    int value();
+  }
+
   @Data
   public static class Employee {
+    @Ordinal(0)
     public final int id;
+    @Ordinal(1)
     public final long empno;
```

为什么更安全：

```text
该类 ID flaky 与字段反射/字段顺序有关。
StabilityOps 不让 LLM 给查询链随意追加 forEachOrdered 或打印副作用。
executor 只执行受保护的 ordinal annotation operator，把不稳定的字段发现顺序转化为显式 ordinal 元数据。
```

### Global State Mutation With Swallowed Exceptions

来源：

```text
audit_id: flakyfix_style_unsafe_materialized_049
method: FlakyFix-style
sample: apache__shardingsphere-elasticjob__4c64eccb9bfc
category: NIO
patch: docs/generated/safety_audit_patches/flakyfix_style_unsafe_materialized_049.patch
```

关键片段：

```diff
+    private static void resetGsonFactory() {
+        try {
+            java.lang.reflect.Field field = GsonFactory.class.getDeclaredField("gson");
+            field.setAccessible(true);
+            field.set(null, null);
+        } catch (Exception e) {
+            // Ignore, as this is just a best-effort cleanup
+        }
+    }
```

问题：

```text
该 patch 通过反射修改全局静态状态，并吞掉异常。
即使方向上像是在处理 NIO，也缺少 guard、调用点和失败处理保证。
因此它属于 suspicious free-form patch，而不是可接受的确定性修复。
```

StabilityOps 的处理：

```text
同一样本:
  sample: apache__shardingsphere-elasticjob__4c64eccb9bfc
  StabilityOps action: NIO_STATIC_FIELD_RESET
  typed parameters:
    GsonFactory.gson -> ASSIGN_NULL
  executor result: materialized but target validation failed
  final decision: not repaired
```

StabilityOps 生成的关键片段：

```diff
     @Test
     public void assertRegisterTypeAdapter() {
+        GsonFactory.gson = null;
         Gson beforeRegisterGson = GsonFactory.getGson();
```

为什么仍然更安全：

```text
StabilityOps 没有生成反射 helper，也没有吞掉异常。
LLM 只能请求 ASSIGN_NULL 这种 typed reset operation，executor 生成一条直接赋值语句。
该 patch 没通过 validation，因此不会被计为成功修复；失败被记录为 validation_failure，而不是被包装成 plausible patch。
```

## How StabilityOps Avoids These Cases

StabilityOps 不让 LLM 直接输出上述 free-form patch。LLM 只能选择 typed DSL action，例如：

```text
NIO_STATIC_FIELD_RESET
ID_JSON_READTREE_ASSERT
OD_VIC_ORMLITE_DROP_TABLE_BEFORE
```

然后 executor 检查 typed parameters 和 guard，再确定性生成 patch。不能满足 guard 时系统拒绝，而不是让模型临时编造 helper、sleep 或 placeholder 修改。

论文中可用的核心表述：

```text
The patch safety filter rejects patches that either weaken the test oracle or do not constitute meaningful repairs, including fixed sleeps, trivial assertions, formatting-only edits, placeholder patches, and unconstrained helper/state mutations.
```
