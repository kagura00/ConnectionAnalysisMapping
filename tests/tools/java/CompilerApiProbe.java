import com.sun.source.tree.ClassTree;
import com.sun.source.tree.CompilationUnitTree;
import com.sun.source.tree.MethodTree;
import com.sun.source.util.JavacTask;
import com.sun.source.util.TreePathScanner;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Set;
import java.util.TreeSet;
import javax.tools.JavaCompiler;
import javax.tools.JavaFileObject;
import javax.tools.StandardJavaFileManager;
import javax.tools.ToolProvider;

public final class CompilerApiProbe {
    private CompilerApiProbe() {}

    public static void main(String[] args) throws Exception {
        Path root = Path.of(args[0]);
        List<Path> sources;
        try (var paths = Files.walk(root)) {
            sources = paths.filter(path -> path.toString().endsWith(".java")).sorted().toList();
        }

        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        if (compiler == null) {
            throw new IllegalStateException("a JDK compiler is required");
        }
        Set<String> names = new TreeSet<>();
        try (StandardJavaFileManager fileManager = compiler.getStandardFileManager(null, null, null)) {
            Iterable<? extends JavaFileObject> units = fileManager.getJavaFileObjectsFromPaths(sources);
            JavacTask task = (JavacTask) compiler.getTask(
                    null,
                    fileManager,
                    null,
                    List.of("-proc:none"),
                    null,
                    units);
            Iterable<? extends CompilationUnitTree> parsed = task.parse();
            new TreePathScanner<Void, Void>() {
                @Override
                public Void visitClass(ClassTree node, Void unused) {
                    names.add(node.getSimpleName().toString());
                    return super.visitClass(node, unused);
                }

                @Override
                public Void visitMethod(MethodTree node, Void unused) {
                    names.add(node.getName().toString());
                    return super.visitMethod(node, unused);
                }
            }.scan(parsed, null);
        }
        System.out.println(String.join("\n", names));
    }
}
