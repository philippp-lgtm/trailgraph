# Giving an app a role

Roles let apps of one project differ. Only a maintainer changes policies and creates tokens.

1. Declare the role under `roles:` in the installation policy, name it in grants
   (`principal: {role: <role>}` with `entrypoint: server`) and in deny rules
   (`role: [<role>]`). A grant for a role that differs from the rest carries one or more of
   the keys below; each needs graphs named one by one (`graphs: [<graph>]`, not `"*"`), because
   the terms belong to an ontology:

   ```yaml
   grants:
     - entrypoint: server
       principal: {role: <role>}
       graphs: [<graph>]
       level: read
       reads: [<prefix>:<Type>, <prefix>:*]
       writes:
         <prefix>:<Type>:
           create: [<attribute>]
           update: [<attribute>]
           link: [<prefix>:<relation>]
           transition: true
       markings: [<prefix>:<marking>]
   ```

   - `writes` (needs `level: read`; `level: write` already allows every write) says exactly
     what the role may write. It maps concrete types (no `prefix:*`; an abstract type or one
     with subclasses is `policy.invalid`) to verbs: `create` and `update` take `true` (every
     attribute) or a non-empty list of attribute names, `link` and `unlink` `true` or a list of
     relation CURIEs, `transition` `true` or a list of lifecycle attributes, `supersede`,
     `retract` and `alias` are `true`. An absent or `false` verb is not granted, and a type
     grants at least one verb. An update never touches a lifecycle attribute (that is
     `transition`), and a `create` list must name every attribute the type requires, else the
     grant is unusable (`policy diff` warns `admin.policy_unusable_write`).
   - `reads` (needs `level: read`) lists the only types the role sees: concrete type CURIEs
     (abstract or with subclasses is `policy.invalid`) or `<prefix>:*` for every type of a
     module, a non-empty list. The rest of the graph reads as if absent. With `writes`, every
     written type must be in `reads`.
   - `markings` (any level) lists the marking CURIEs (`<prefix>:<name>`) the role holds. A
     marked attribute is hidden from every reader until a grant names its marking; a marking
     the pinned ontology does not declare only warns (`admin.policy_unknown_marking`). There
     is no deny rule on a marking.
   - Rights add up: a token with several roles may do what any of them allows, and a deny rule
     or scope still narrows the result.
2. `graph-admin policy diff <file>`, then `graph-admin policy apply <file> --reason "<why>"`.
3. Give the role to an app token with `graph-admin token create <name> --role <role>` or
   `graph-admin token role add <name> <role>` (`token role remove` takes it away), or to a
   client with `graph-admin enroll create --project <project> --role <role>`. App tokens only;
   `admin.role_unknown` means the policy does not declare the role.
4. `graph-admin policy explain --graph <graph> --role <role>` shows what a token with the role
   gets.
